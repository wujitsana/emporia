"""`emporia agent register-8004`: the registration file, the call, the receipt and the refusals. Fixtures were recorded
from Base Sepolia on 11-16 (a real mint receipt, the field names of real registrations); no test touches a network."""
import copy
import http.client
import json
import pathlib
import re
import shutil
import subprocess
import threading
import uuid

import pytest

from emporia import agent_card_cli as card
from emporia import browser_sign as bs
from emporia import chain_link, chain_read, chain_register, erc8004, mcp_server
from emporia import erc8004_register as R
from emporia.networks import ERC8004_REGISTRIES
from tests.test_x_link_cli import FakeTarget

pytestmark = pytest.mark.skipif(not erc8004.available(), reason="eth-account is not installed")
FXD = pathlib.Path(__file__).parent / "fixtures"
REAL = json.loads((FXD / "erc8004/register_receipt.json").read_text())
FILES = json.loads((FXD / "erc8004/real_registrations.json").read_text())["files"]
SEPOLIA = "eip155:84532"
REGISTRY = ERC8004_REGISTRIES[SEPOLIA]
STABLE = "cd" * 32
TX = REAL["receipt"]["transactionHash"]
OWNER = "0x" + "11" * 20
relay_url = "https://relay.example.org"


def receipt(**over):
    r = copy.deepcopy(REAL["receipt"])
    r.update(over)
    return r


# -- the call --------------------------------------------------------------------------------------------------
def test_selectors_and_topics_are_the_keccak_of_their_signatures():
    from eth_utils import keccak
    for sig, sel in R.SELECTORS.items():
        assert keccak(text=sig)[:4] == sel, sig
    for sig, topic in R.TOPICS.items():
        assert "0x" + keccak(text=sig).hex() == topic, sig


def test_the_recorded_mint_called_the_selector_we_use():
    assert REAL["tx_input_selector"] == "0x" + R.SEL_REGISTER_URI.hex()


def test_registration_file_has_the_fields_real_registrations_use():
    f = R.registration_file("kestrel", "Wanders the Agora.", relay_url + "/", STABLE, True)
    real_keys = set(FILES["b"])
    assert real_keys - {"image"} <= set(f), "every field a real full registration has, bar the image"
    assert f["type"] == FILES["b"]["type"] and f["active"] is True and f["x402Support"] is True
    assert "image" in real_keys and f.get("image") is None, "no image by default"
    assert f["services"] == [{"name": "Emporia", "endpoint": "https://relay.example.org/agents/kestrel", "stableId": STABLE}]
    assert f["description"] == "Wanders the Agora." and f["registrations"] == [] and f["supportedTrust"] == []


def test_the_name_in_the_endpoint_is_percent_encoded_and_a_missing_bio_gets_a_plain_one():
    f = R.registration_file("a b/c", "", relay_url, "0x" + STABLE, False)
    assert f["services"][0]["endpoint"].endswith("/agents/a%20b%2Fc") and f["services"][0]["stableId"] == STABLE
    assert f["description"] == "An Emporia agent, a b/c." and f["x402Support"] is False


@pytest.mark.parametrize("sid,relay", [("", relay_url), ("zz" * 32, relay_url), (STABLE, "")])
def test_no_stable_id_or_relay_means_no_registration(sid, relay):
    with pytest.raises(R.RegisterError):
        R.registration_file("kestrel", "", relay, sid, False)


def test_data_uri_round_trips_and_is_deterministic():
    f = R.registration_file("kestrel", "Wanders.", relay_url, STABLE, True)
    uri = R.data_uri(f)
    assert uri.startswith("data:application/json;base64,") and R.data_uri(f) == uri
    assert R.decode_data_uri(uri) == f
    assert R.decode_data_uri("https://example.org/a.json") is None
    assert R.decode_data_uri("data:application/json;base64,!!!") is None
    assert R.decode_data_uri("data:application/json;base64," + "W10=") is None      # a JSON list is not a registration


def test_plain_json_registrations_are_still_read_percent_and_hash_included():
    f = R.registration_file("kestrel", "100% fair, room #3", relay_url, STABLE, True)
    plain = "data:application/json;utf8," + json.dumps(f).replace("%", "%25").replace("#", "%23")
    assert R.decode_data_uri(plain) == f and R.names_agent(plain, STABLE)
    from emporia.console.chain import parse_data_uri
    assert json.loads(parse_data_uri(plain, 65536)[1]) == f                        # the console reads it the same


def test_a_registration_over_the_size_cap_is_refused():
    f = R.registration_file("kestrel", "x", relay_url, STABLE, True)
    f["description"] = "y" * R.MAX_URI_BYTES
    with pytest.raises(R.RegisterError):
        R.data_uri(f)


def test_calldata_is_the_selector_then_the_abi_encoded_uri():
    from eth_abi import decode
    uri = R.data_uri(R.registration_file("kestrel", "Wanders.", relay_url, STABLE, True))
    data = R.calldata(uri)
    assert data[:4] == bytes.fromhex("f2c298be") and decode(["string"], data[4:]) == (uri,)
    assert len(data) == 4 + 32 + 32 + (len(uri) + 31) // 32 * 32


# -- the receipt -----------------------------------------------------------------------------------------------
def test_the_recorded_real_receipt_gives_the_agent_number_and_owner():
    got = R.agent_from_receipt(REAL["receipt"], REGISTRY, TX)
    assert got["agent_id"] == REAL["agent_id"] == "10727"
    assert got["owner"] == "0xcea60a35c0d78e2daa2bb754868cb90defd7af04"
    assert got["agent_uri"] == "https://example.org/agent.json"
    assert got["block_number"] == int(REAL["receipt"]["blockNumber"], 16)


def test_a_receipt_without_the_registered_event_still_needs_it():
    logs = [l for l in REAL["receipt"]["logs"] if l["topics"][0] != R.TOPIC_REGISTERED]      # only Transfer and others
    with pytest.raises(R.RegisterError, match="registered no agent"):
        R.agent_from_receipt(receipt(logs=logs), REGISTRY, TX)


@pytest.mark.parametrize("over,words", [
    ({"status": "0x0"}, "failed on-chain"),
    ({"transactionHash": "0x" + "33" * 32}, "another transaction"),
])
def test_a_failed_misdirected_or_foreign_receipt_is_refused(over, words):
    with pytest.raises(R.RegisterError, match=words):
        R.agent_from_receipt(receipt(**over), REGISTRY, TX)


def test_no_receipt_is_a_plain_message():
    with pytest.raises(R.RegisterError, match="no receipt"):
        R.agent_from_receipt(None, REGISTRY, TX)


def test_a_registered_event_from_another_contract_is_ignored():
    """A contract that imitates the event cannot make a number: only the registry's own log counts."""
    real = [l for l in REAL["receipt"]["logs"] if l["topics"][0] == R.TOPIC_REGISTERED][0]
    fake = {**real, "address": "0x" + "44" * 20, "topics": [real["topics"][0], "0x" + "00" * 31 + "07", real["topics"][2]]}
    logs = [l for l in REAL["receipt"]["logs"] if l["topics"][0] != R.TOPIC_REGISTERED] + [fake]
    with pytest.raises(R.RegisterError, match="registered no agent"):
        R.agent_from_receipt(receipt(logs=logs), REGISTRY, TX)


def test_two_registrations_in_one_transaction_are_not_guessed_between():
    real = [l for l in REAL["receipt"]["logs"] if l["topics"][0] == R.TOPIC_REGISTERED][0]
    with pytest.raises(R.RegisterError, match="more than one"):
        R.agent_from_receipt(receipt(logs=REAL["receipt"]["logs"] + [real]), REGISTRY, TX)


def test_a_mint_transfer_that_disagrees_with_the_event_is_not_trusted():
    logs = copy.deepcopy(REAL["receipt"]["logs"])
    for l in logs:
        if l["topics"][0] == R.TOPIC_TRANSFER:
            l["topics"][3] = "0x" + "00" * 31 + "08"
    with pytest.raises(R.RegisterError, match="disagree"):
        R.agent_from_receipt(receipt(logs=logs), REGISTRY, TX)


def test_garbled_log_data_is_not_a_crash():
    logs = copy.deepcopy(REAL["receipt"]["logs"])
    for l in logs:
        if l["topics"][0] == R.TOPIC_REGISTERED:
            l["data"] = "0xzz"
    with pytest.raises(R.RegisterError, match="registered no agent"):
        R.agent_from_receipt(receipt(logs=logs), REGISTRY, TX)


def test_names_agent_needs_the_emporia_service_with_this_stable_id():
    f = R.registration_file("kestrel", "", relay_url, STABLE, False)
    assert R.names_agent(R.data_uri(f), STABLE) and R.names_agent(R.data_uri(f), "0x" + STABLE.upper())
    assert not R.names_agent(R.data_uri(f), "ef" * 32)
    other = copy.deepcopy(f)
    other["services"][0]["name"] = "web"
    assert not R.names_agent(R.data_uri(other), STABLE)
    assert not R.names_agent("https://example.org/a.json", STABLE) and not R.names_agent(R.data_uri(f), "")
    junk = copy.deepcopy(f)
    junk["services"] = [None, "x", {"name": "Emporia", "stableId": 5}]
    assert not R.names_agent(R.data_uri(junk), STABLE)


@pytest.mark.parametrize("chain,ok", [(SEPOLIA, True), ("eip155:8453", False), ("eip155:4663", False),
                                      ("eip155:46630", False), ("solana:abc", False)])
def test_only_base_sepolia_is_open(chain, ok):
    assert (R.chain_problem(chain) == "") is ok
    if not ok and chain in ERC8004_REGISTRIES:
        assert "release owner's go" in R.chain_problem(chain)


# -- the agent's side, against a fake chain ---------------------------------------------------------------------
class FakeReg:
    """A reader that knows one registry: `register` answers an id, tokens have owners and URIs, a receipt is held."""

    def __init__(self, caip2=SEPOLIA, safe=1_000, finalized=1_000):
        self.caip2, self.block_number, self.block_hash = caip2, safe, "0x" + "ab" * 32
        self.fin = finalized
        self.registry = ERC8004_REGISTRIES[caip2]
        self.owners, self.uris, self.receipts, self.calls = {}, {}, {}, []
        self.sim_id, self.gas, self.revert, self.down = 10729, 523_000, False, False
        self.block_hashes = {}

    def simulate(self, to, data, sender):
        self.calls.append(("simulate", to, bytes(data), sender))
        if self.down:
            raise chain_read.ChainUnknown("down")
        if self.revert:
            raise chain_read.ChainRevert("no")
        return self.sim_id.to_bytes(32, "big")

    def estimate_gas(self, to, data, sender):
        self.calls.append(("gas", to, bytes(data), sender))
        return self.gas

    def receipt(self, tx_hash):
        if self.down:
            raise chain_read.ChainUnknown("down")
        return self.receipts.get(tx_hash)

    def block_hash_at(self, number):
        return self.block_hashes.get(number, "0x" + "00" * 32)

    def call(self, to, data):
        sel, body = data[:4], data[4:]
        n = int.from_bytes(body[:32], "big")
        if sel == erc8004.SEL_OWNER_OF:
            if n not in self.owners:
                raise chain_read.ChainRevert("none")
            return bytes(12) + bytes.fromhex(self.owners[n][2:])
        if sel == R.SEL_TOKEN_URI:
            from eth_abi import encode
            return encode(["string"], [self.uris[n]])
        raise chain_read.ChainRevert("no such function")


def minted(fake, stable=STABLE, name="kestrel", tamper=None):
    """Give the fake chain the recorded mint, with a registration that names `stable` (or `tamper`'s)."""
    rec = receipt()
    blk = int(rec["blockNumber"], 16)
    fake.block_number = blk + 5
    fake.block_hashes[blk] = rec["blockHash"].lower()
    fake.receipts[TX] = rec
    owner = [l for l in rec["logs"] if l["topics"][0] == R.TOPIC_REGISTERED][0]["topics"][2][-40:]
    fake.owners[10727] = "0x" + owner
    f = R.registration_file(name, "Wanders.", relay_url, stable, False)
    fake.uris[10727] = R.data_uri(tamper(f) if tamper else f)
    return fake


@pytest.fixture
def target(monkeypatch):
    name = f"regagent_{uuid.uuid4().hex[:6]}"
    t = FakeTarget(name)
    monkeypatch.setattr(card, "_Target", lambda n: t)
    monkeypatch.setattr(mcp_server, "AGENT_ID", name)
    monkeypatch.setattr(mcp_server, "RELAY_URL", relay_url)
    monkeypatch.setattr(mcp_server, "_signing_identity", lambda aid: STABLE)
    monkeypatch.setattr(chain_register, "_about", lambda: "Wanders the Agora.")
    monkeypatch.setattr(chain_register, "_x402_support", lambda: True)
    return t


@pytest.fixture
def fake(monkeypatch):
    f = FakeReg()
    monkeypatch.setattr(chain_register, "_reader", lambda caip2, read=None, tag="finalized": f if tag != "finalized" or read is None else read)
    return f


def test_build_gives_the_call_the_agent_uri_the_simulation_and_the_gas(target, fake):
    out = chain_register.build("base-sepolia", stable=STABLE)
    assert out["chain_id"] == 84532 and out["registry"] == REGISTRY and out["selector"] == "0xf2c298be"
    assert out["calldata"] == "0x" + R.calldata(out["agent_uri"]).hex()
    assert R.decode_data_uri(out["agent_uri"]) == out["file"]
    assert out["would_register_id"] == "10729" and out["gas_estimate"] == 523_000
    assert fake.calls[0][1] == REGISTRY and fake.calls[0][3] == chain_register.PLACEHOLDER_SENDER
    assert out["local_relay"] is False


def test_the_sender_for_the_estimate_is_the_one_given(target, fake):
    chain_register.build("base-sepolia", OWNER, stable=STABLE)
    assert fake.calls[0][3] == OWNER
    with pytest.raises(chain_link.LinkError):
        chain_register.build("base-sepolia", "0x12", stable=STABLE)


def test_a_local_relay_is_flagged(target, fake):
    assert chain_register.build("base-sepolia", stable=STABLE, relay="http://localhost:8088")["local_relay"] is True


@pytest.mark.parametrize("chain", ["base", "robinhood", "robinhood-testnet", "tempo"])
def test_every_other_chain_is_refused_with_a_plain_message(target, fake, chain):
    with pytest.raises(chain_link.LinkError, match="release owner's go|no ERC-8004 registry"):
        chain_register.build(chain, stable=STABLE)
    with pytest.raises(chain_link.LinkError, match="release owner's go|no ERC-8004 registry"):
        chain_register.confirm(chain, TX, stable=STABLE)
    assert fake.calls == []


def test_a_registry_that_would_refuse_or_a_dead_rpc_sends_the_person_nowhere(target, fake):
    fake.revert = True
    with pytest.raises(chain_link.LinkError, match="would refuse"):
        chain_register.build("base-sepolia", stable=STABLE)
    fake.revert, fake.down = False, True
    with pytest.raises(chain_link.LinkError, match="could not be asked"):
        chain_register.build("base-sepolia", stable=STABLE)


def test_confirm_finds_the_agent_and_the_two_way_link(target, fake):
    minted(fake)
    out = chain_register.confirm("base-sepolia", TX, read=fake, finalized=fake, stable=STABLE)
    assert out["agent_id"] == "10727" and out["two_way"] is True and out["owner"].startswith("0xcea6")


@pytest.mark.parametrize("bad", ["0x12", "nothex", "0x" + "g" * 64, ""])
def test_a_bad_hash_is_refused_before_any_read(target, fake, bad):
    with pytest.raises(chain_link.LinkError, match="transaction hash"):
        chain_register.confirm("base-sepolia", bad, read=fake, stable=STABLE)


def test_a_transaction_not_mined_yet_is_said_so(target, fake):
    with pytest.raises(chain_link.LinkError, match="not mined yet"):
        chain_register.confirm("base-sepolia", "0x" + "55" * 32, read=fake, stable=STABLE)


def test_a_receipt_newer_than_the_safe_block_waits(target, fake):
    minted(fake)
    fake.block_number = int(REAL["receipt"]["blockNumber"], 16) - 1
    with pytest.raises(chain_link.LinkError, match="safe block"):
        chain_register.confirm("base-sepolia", TX, read=fake, stable=STABLE)


def test_a_receipt_whose_block_is_not_the_chains_is_not_trusted(target, fake):
    minted(fake)
    fake.block_hashes.clear()
    with pytest.raises(chain_link.LinkError, match="not the chain's block"):
        chain_register.confirm("base-sepolia", TX, read=fake, stable=STABLE)


def test_a_registration_through_a_smart_account_counts_by_the_registrys_own_log(target, fake):
    # a wallet's smart account (EIP-7702, "Redeem Delegations") sends to its delegation contract, which calls register
    minted(fake)
    fake.receipts[TX] = receipt(to="0x" + "66" * 20)
    assert chain_register.confirm("base-sepolia", TX, read=fake, stable=STABLE)["agent_id"]


def test_a_transaction_where_only_another_contract_logged_a_registration_is_refused():
    shaped = [{**l, "address": "0x" + "66" * 20} for l in REAL["receipt"]["logs"]]
    with pytest.raises(R.RegisterError, match="registered no agent"):
        R.agent_from_receipt(receipt(to="0x" + "66" * 20, logs=shaped), REGISTRY, TX)


def test_a_token_that_has_moved_since_is_not_followed(target, fake):
    minted(fake)
    fake.owners[10727] = OWNER
    with pytest.raises(chain_link.LinkError, match="owner of that token is not the one"):
        chain_register.confirm("base-sepolia", TX, read=fake, finalized=fake, stable=STABLE)


def test_an_agent_uri_that_names_another_agent_is_not_linked(target, fake):
    minted(fake, stable="ef" * 32)
    with pytest.raises(chain_link.LinkError, match="does not name this agent"):
        chain_register.confirm("base-sepolia", TX, read=fake, finalized=fake, stable=STABLE)
    minted(fake, tamper=lambda f: {**f, "services": []})
    with pytest.raises(chain_link.LinkError, match="does not name this agent"):
        chain_register.confirm("base-sepolia", TX, read=fake, finalized=fake, stable=STABLE)


# -- the command ------------------------------------------------------------------------------------------------
def test_the_command_prints_the_call_in_plain_words_and_sends_nothing(target, fake, capsys):
    assert card.register_8004(target.name, "base-sepolia") == 0
    out = capsys.readouterr().out
    for want in ("Emporia sends nothing and spends nothing", "chain id:   84532", REGISTRY, "0xf2c298be",
                 "data:application/json;base64,", "mint agent #10729", "about 523,000", "write-as-proxy",
                 "--browser", "--tx 0xHASH", "stable id:   " + STABLE, "What goes on-chain:",
                 "Sending it yourself (technical"):
        assert want in out, want
    assert [c[0] for c in fake.calls] == ["simulate", "gas"], "only reads"


@pytest.mark.parametrize("chain", ["base", "robinhood"])
def test_the_command_refuses_mainnet_and_every_other_chain(target, fake, capsys, chain):
    assert card.register_8004(target.name, chain) == 1
    assert "release owner's go" in capsys.readouterr().out
    assert card.register_8004(target.name, chain, TX) == 1
    assert "release owner's go" in capsys.readouterr().out and fake.calls == []


def test_the_command_is_refused_in_an_agent_session_even_with_yes(target, fake, capsys, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "s1")
    for kw in ({}, {"browser": True, "yes": True}, {"tx": TX, "yes": True}):
        assert card.register_8004(target.name, "base-sepolia", **kw) == 1
        assert "person's to change" in capsys.readouterr().out
    assert fake.calls == []


def test_it_is_refused_while_a_turn_is_running_and_for_an_unknown_agent(target, fake, capsys):
    target.live = "relay-started"
    assert card.register_8004(target.name, "base-sepolia") == 1
    assert "turn is running" in capsys.readouterr().out
    target.live = ""
    target.found = False
    assert card.register_8004("nobody", "base-sepolia") == 1


def test_browser_and_tx_together_and_a_missing_chain_are_refused(target, fake, capsys):
    assert card.register_8004(target.name, "base-sepolia", TX, browser=True) == 1
    assert "give one of them" in capsys.readouterr().out
    assert card.register_8004(target.name, "") == 1
    assert card.register_8004(target.name, "base-sepolia", "0x12") == 1
    assert "transaction hash" in capsys.readouterr().out


def test_tx_reads_the_receipt_and_hands_on_to_link_8004_once_the_chain_is_final(target, fake, monkeypatch, capsys):
    minted(fake)
    handed = []
    monkeypatch.setattr(chain_register, "_reader", lambda caip2, read=None, tag="finalized": fake)
    monkeypatch.setattr(card, "link_8004", lambda *a, **k: handed.append(a) or 0)
    fake.block_number = int(REAL["receipt"]["blockNumber"], 16) + 5     # safe and finalized are past it
    assert card.register_8004(target.name, "base-sepolia", TX) == 0
    out = capsys.readouterr().out
    assert "ERC-8004 #10727" in out and "Two-way" in out
    assert handed == [(target.name, "base-sepolia", "10727", "", "", 0, False, False, False, False, 0, "auto")]
    assert "Done." in out and "https://sepolia.basescan.org/tx/" + TX in out          # the closing summary
    assert f"https://sepolia.basescan.org/nft/{REGISTRY}/10727" in out


def test_tx_before_the_chain_is_final_prints_the_link_command_instead(target, fake, monkeypatch, capsys):
    minted(fake)
    handed = []
    monkeypatch.setattr(card, "link_8004", lambda *a, **k: handed.append(a) or 0)
    blk = int(REAL["receipt"]["blockNumber"], 16)
    fin = FakeReg(finalized=blk - 500)
    fin.block_number = blk - 500
    monkeypatch.setattr(chain_register, "_reader",
                        lambda caip2, read=None, tag="finalized": fin if tag == "finalized" else fake)
    assert card.register_8004(target.name, "base-sepolia", TX) == 0
    out = capsys.readouterr().out
    assert handed == [] and "finalized block, which has not reached yours yet" in out
    assert f"emporia agent link-8004 {target.name} --chain base-sepolia --agent-id 10727" in out and "minutes" in out


def test_an_agent_uri_for_another_agent_stops_the_command(target, fake, monkeypatch, capsys):
    minted(fake, stable="ef" * 32)
    monkeypatch.setattr(card, "link_8004", lambda *a, **k: pytest.fail("must not link"))
    assert card.register_8004(target.name, "base-sepolia", TX) == 1
    assert "does not name this agent" in capsys.readouterr().out


# -- --browser: the page that has the person's wallet send the call ---------------------------------------------
TXD = {"chain_id": 84532, "to": REGISTRY, "data": "0xf2c298be" + "00" * 32}


def send_page(**kw):
    return bs.SendPage(TXD, chain_label="Base Sepolia (testnet)", agent_name="kestrel", summary="Sends register.", **kw).start()


def request(p, method, path, body=None, headers=None):
    c = http.client.HTTPConnection(bs.HOST, p.port, timeout=5)
    h = {"Host": f"{bs.HOST}:{p.port}", "Content-Type": "application/json", **(headers or {})}
    c.request(method, path, body=json.dumps(body) if isinstance(body, (dict, list)) else body, headers=h)
    r = c.getresponse()
    out = (r.status, dict((k.lower(), v) for k, v in r.getheaders()), r.read())
    c.close()
    return out


def test_the_send_page_shows_the_call_under_the_same_strict_policy_and_takes_one_hash():
    p = send_page()
    t = threading.Thread(target=lambda: _quiet(p), daemon=True)
    t.start()
    try:
        status, headers, body = request(p, "GET", "/" + p.token)
        assert status == 200 and request(p, "GET", "/wrong")[0] == 404
        html = body.decode()
        assert "Sends register." in html and REGISTRY in html and "0xf2c298be" in html and "confirm one transaction" in html
        csp = headers["content-security-policy"]
        assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "script-src 'nonce-" in csp
        assert request(p, "POST", f"/{p.token}/sign", {"signature": "0x" + "ab" * 65, "account": OWNER})[0] == 400, \
            "a signature is not a transaction hash"
        assert request(p, "POST", f"/{p.token}/sign", {"tx_hash": "0x12", "account": OWNER})[0] == 400
        assert p.result is None
        assert request(p, "POST", f"/{p.token}/sign", {"tx_hash": "0x" + "AB" * 32, "account": OWNER})[0] == 200
        assert p.result == {"tx_hash": "0x" + "ab" * 32, "account": OWNER}
    finally:
        t.join(5)


def _quiet(p):
    try:
        p.wait()
    except bs.BrowserSignError:
        pass


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
HARNESS = FXD / "browser_sign" / "harness.mjs"


def run_send_page(tmp_path, wallet, post="ok"):
    p = send_page()
    t = threading.Thread(target=lambda: _quiet(p), daemon=True)
    t.start()
    try:
        html = request(p, "GET", "/" + p.token)[2].decode()
    finally:
        p.timeout = 0
        t.join(3)
    script = re.search(r'<script nonce="[^"]+">(.*)</script>', html, re.S).group(1)
    f = tmp_path / "page.js"
    f.write_text(script)
    scenario = {"tx": TXD, "chain_label": "Base Sepolia (testnet)", "wallet": wallet, "announce": True, "post": post}
    out = subprocess.run(["node", str(HARNESS), str(f), json.dumps(scenario)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
def test_the_page_switches_chain_then_has_the_wallet_send_and_posts_only_the_hash(tmp_path):
    log = run_send_page(tmp_path, {"chain": 1, "accounts": [OWNER], "switch": "ok", "send": "ok"})
    assert log["calls"] == ["eth_requestAccounts", "eth_chainId", "wallet_switchEthereumChain", "eth_chainId", "eth_sendTransaction"]
    assert log["sent"] == {"from": OWNER, "to": REGISTRY, "data": TXD["data"], "value": "0x0"}
    (post,) = log["posts"]
    assert post["body"] == {"tx_hash": "0x" + "cd" * 32, "account": OWNER} and log["statusClass"] == "good"


@needs_node
def test_declining_in_the_wallet_sends_nothing_and_a_refusing_terminal_never_allows_a_second_send(tmp_path):
    log = run_send_page(tmp_path, {"chain": 84532, "accounts": [OWNER], "switch": "ok", "send": "refuse"})
    assert log["posts"] == [] and "declined" in log["status"] and log["signDisabled"] is False
    log = run_send_page(tmp_path, {"chain": 84532, "accounts": [OWNER], "switch": "ok", "send": "ok"}, post="bad")
    assert "was sent" in log["status"] and "--tx" in log["status"] and log["signDisabled"] is True


@needs_node
def test_a_wallet_that_will_not_switch_is_not_asked_to_send(tmp_path):
    log = run_send_page(tmp_path, {"chain": 1, "accounts": [OWNER], "switch": "refuse", "send": "ok"})
    assert "eth_sendTransaction" not in log["calls"] and log["posts"] == [] and "did not switch" in log["status"]


def test_browser_sends_then_continues_as_tx(target, fake, monkeypatch, capsys):
    """The CLI opens the page, a stand-in wallet posts a hash, and the CLI reads the chain itself from there."""
    monkeypatch.setattr("emporia.cli._persons_terminal", lambda: True)
    monkeypatch.setattr("emporia.cli.prompt", lambda q, d="": "y")
    minted(fake)
    fake.block_number = int(REAL["receipt"]["blockNumber"], 16) + 5
    monkeypatch.setattr(chain_register, "_reader", lambda caip2, read=None, tag="finalized": fake)
    handed = []
    monkeypatch.setattr(card, "link_8004", lambda *a, **k: handed.append(a) or 0)
    seen, threads = {}, []

    def opener(url):
        def go():
            m = re.match(r"http://(?:127\.0\.0\.1|emporia\.localhost):(\d+)(/.+)", url)
            port, path = int(m.group(1)), m.group(2)
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
            body = c.getresponse().read().decode()
            doc = json.loads(re.search(r'id="page-data">(.*?)</script>', body, re.S).group(1))
            seen["tx"] = doc["tx"]
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("POST", path.rstrip("/") + "/sign", body=json.dumps({"tx_hash": TX, "account": OWNER}),
                      headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"})
            seen["status"] = c.getresponse().status
        threads.append(threading.Thread(target=go, daemon=True))
        threads[-1].start()
    monkeypatch.setattr("webbrowser.open", opener)
    assert card.register_8004(target.name, "base-sepolia", browser=True) == 0
    threads[0].join(5)
    out = capsys.readouterr().out
    assert seen["status"] == 200 and seen["tx"]["to"] == REGISTRY and seen["tx"]["chain_id"] == 84532
    assert seen["tx"]["data"] == chain_register.build("base-sepolia", stable=STABLE)["calldata"]
    assert f"transaction {TX} from {OWNER}" in out and handed, "it went on to link-8004 for the new agent"
    assert "What goes on-chain:" in out and "calldata:" not in out and "agentURI:" not in out   # words only


def test_the_terminal_guard_blocks_register_for_an_agent():
    from hermes_plugin.emporia import key_guard as g
    for cmd in ("emporia agent register-8004 shop --chain base-sepolia",
                "emporia agent register-8004 shop --chain base-sepolia --browser --yes",
                "emporia agent register-8004 shop --chain base-sepolia --tx 0x" + "ab" * 32):
        assert g.pre_tool_call("terminal", {"command": cmd})["action"] == "block", cmd


def test_register_is_not_an_agent_tool_and_touches_no_money_path():
    names = {n for n in dir(mcp_server) if "8004" in n}
    assert not any("register" in n for n in names), names
    assert "register-8004" not in " ".join(getattr(mcp_server, "MONEY_TOOLS", ()))


def test_tx_wait_polls_until_final_then_links_with_wait(target, fake, monkeypatch, capsys):
    minted(fake)
    handed, sleeps = [], []
    blk = int(REAL["receipt"]["blockNumber"], 16)
    fin = FakeReg(finalized=blk - 500)
    fin.block_number = blk - 500
    monkeypatch.setattr(card.time, "sleep", lambda s: (sleeps.append(s), setattr(fin, "block_number", blk + 1)))
    monkeypatch.setattr(chain_register, "_reader",
                        lambda caip2, read=None, tag="finalized": fin if tag == "finalized" else fake)
    monkeypatch.setattr(card, "link_8004", lambda *a, **k: handed.append(a) or 0)
    assert card.register_8004(target.name, "base-sepolia", TX, wait=True) == 0
    out = capsys.readouterr().out
    assert sleeps == [15.0] and "waiting for the transaction" in out
    assert handed == [(target.name, "base-sepolia", "10727", "", "", 0, False, False, True, False, 0, "auto")]


def test_while_it_waits_the_number_is_said_at_once_with_minutes_to_go(target, fake, monkeypatch, capsys):
    minted(fake)
    blk = int(REAL["receipt"]["blockNumber"], 16)
    fake.block_number = blk - 300                                    # the safe block lags 300 blocks (~10 min)
    def tick(s):
        fake.block_number = blk + 1
    monkeypatch.setattr(card.time, "sleep", tick)
    monkeypatch.setattr(card, "link_8004", lambda *a, **k: 0)
    assert card.register_8004(target.name, "base-sepolia", TX, wait=True) == 0
    out = capsys.readouterr().out
    assert f"{target.name} is ERC-8004 #10727" in out and "from the receipt: mined, not final yet" in out
    assert "about 10 min" in out and "about 10 min to go (300 blocks)" in out


def test_a_person_who_sent_from_the_page_is_offered_the_wallet_step_next(target, fake, monkeypatch, capsys):
    minted(fake)
    fake.block_number = int(REAL["receipt"]["blockNumber"], 16) + 5
    monkeypatch.setattr(card, "_RECORDED", set())
    monkeypatch.setattr(card, "link_8004", lambda name, chain, aid, *a, **k: card._RECORDED.add((name, aid)) or 0)
    cli = card._cli()
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(cli, "prompt", lambda q, d="": "y")
    asked = []
    monkeypatch.setattr(card, "set_8004_wallet", lambda *a, **k: asked.append((a, k)) or 0)
    assert card._register_after(card._Target(target.name), target.name, "base-sepolia", TX, False, browser=True) == 0
    (a, k), = asked
    assert a[2] == "10727" and k["browser"] is True and k["wait"] is True
    assert "paid at:" in capsys.readouterr().out


def test_a_declined_link_is_not_called_linked_and_no_wallet_step_is_offered(target, fake, monkeypatch, capsys):
    """Release review 2.1 (delta 2, finding 2): link-8004 returns 0 when the person says no; the summary must not
    claim the link, and the wallet step is offered only after a link and only where it can be sent (a browser run)."""
    minted(fake)
    fake.block_number = int(REAL["receipt"]["blockNumber"], 16) + 5
    monkeypatch.setattr(card, "_RECORDED", set())
    monkeypatch.setattr(card, "link_8004", lambda *a, **k: 0)                 # declined: nothing recorded
    cli = card._cli()
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(cli, "prompt", lambda q, d="": "y")
    monkeypatch.setattr(card, "set_8004_wallet", lambda *a, **k: pytest.fail("no wallet step without a link"))
    assert card._register_after(card._Target(target.name), target.name, "base-sepolia", TX, False, browser=True) == 0
    assert "not linked to the card yet" in capsys.readouterr().out
    monkeypatch.setattr(card, "link_8004", lambda name, chain, aid, *a, **k: card._RECORDED.add((name, aid)) or 0)
    monkeypatch.setattr(card, "set_8004_wallet", lambda *a, **k: pytest.fail("the --tx path cannot send it"))
    assert card._register_after(card._Target(target.name), target.name, "base-sepolia", TX, False) == 0
    assert "not linked" not in capsys.readouterr().out


def test_the_not_final_yet_message_says_what_wait_does(target, fake, monkeypatch):
    minted(fake)
    fake.block_number = int(REAL["receipt"]["blockNumber"], 16) - 50
    with pytest.raises(chain_link.LinkError, match="checks every 15 seconds, up to 30 minutes"):
        chain_register.confirm("base-sepolia", TX, read=fake, stable=STABLE)
