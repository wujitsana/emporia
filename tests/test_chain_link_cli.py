"""`emporia agent link-8004 | unlink-8004` against an in-memory chain: what the person is told, the local check,
the signed record that is posted, and the refusals. No network."""
import json
import time
import uuid

import pytest

from emporia import agent_card_cli as card
from emporia import chain_link, erc8004, identity, mcp_server, wire
from emporia.networks import ERC8004_REGISTRIES
from tests.test_erc8004_verify import AGENT, FakeChain, bound_chain, keypair, needs_lib, sign
from tests.test_x_link_cli import FakeTarget

pytestmark = needs_lib
STABLE = "cd" * 32


@pytest.fixture
def target(monkeypatch):
    name = f"linkagent_{uuid.uuid4().hex[:6]}"
    t = FakeTarget(name)
    monkeypatch.setattr(card, "_Target", lambda n: t)
    monkeypatch.setattr(mcp_server, "AGENT_ID", name)
    monkeypatch.setattr(mcp_server, "_signing_identity", lambda aid: STABLE)
    return t


@pytest.fixture
def chain(monkeypatch):
    """One fake chain per test; `chain.owners` etc. are set by the test. Reads go to it, whatever the chain."""
    holder = {}

    def reader(caip2, read=None):
        ch = holder["chain"]
        ch.caip2, ch.registry = caip2, ERC8004_REGISTRIES[caip2]
        return ch
    monkeypatch.setattr(chain_link, "_reader", reader)
    holder["chain"] = FakeChain()
    return holder


def _owned_by_eoa(chain):
    key, addr = keypair()
    chain["chain"].owners[AGENT] = addr
    return key, addr


def _sign(key, addr, caip2="eip155:84532", issued=None):
    issued = issued or int(time.time()) - 5
    cid = int(caip2.split(":")[1])
    return issued, sign(key, cid, ERC8004_REGISTRIES[caip2], AGENT, addr, STABLE, issued=issued)


def test_without_a_signature_it_prints_owner_tier_privacy_and_the_typed_message(target, chain, capsys):
    key, addr = _owned_by_eoa(chain)
    assert card.link_8004(target.name, "base-sepolia", str(AGENT)) == 0
    out = capsys.readouterr().out
    assert addr.lower() in out.lower() and "tier:    owner" in out and "publishes this wallet address" in out
    assert "block 123" in out and "--issued-at" in out
    typed = json.loads(out[out.index("{\n"): out.index("\n}\n") + 2])
    assert typed["domain"] == {"name": "Emporia agent link", "version": "1", "chainId": 84532}
    assert typed["message"]["emporiaStableId"] == "0x" + STABLE and typed["message"]["agentId"] == str(AGENT)
    # the printed message is the one a wallet signs: the same digest the verifier rebuilds
    td = erc8004.typed_data_from_json(typed)
    assert erc8004.agent_link_digest(td) == erc8004.agent_link_digest(
        erc8004.agent_link_typed_data(84532, ERC8004_REGISTRIES["eip155:84532"], AGENT, typed["message"]["signer"],
                                      STABLE, typed["message"]["issuedAt"]))


@pytest.mark.parametrize("who,tier", [("owner", "owner"), ("bound", "bound-owner"), ("controller", "controller"),
                                      ("stranger", "")])
def test_the_ask_says_the_tier_the_card_would_show(target, chain, who, tier):
    key, addr = keypair()
    if who == "owner":
        chain["chain"].owners[AGENT] = addr
    elif who == "bound":
        chain["chain"] = bound_chain(erc8004.STD_ERC721, addr)
    elif who == "controller":
        chain["chain"] = bound_chain(erc8004.STD_ERC721, addr, meta=False)
    else:
        chain["chain"].owners[AGENT] = keypair()[1]
    got = chain_link.ask("base-sepolia", AGENT, addr, stable=STABLE)
    assert got["tier"] == tier
    if not tier:
        assert got["reason"]
    if who == "bound":
        assert got["binding"]["standard"] == "erc721"


def test_the_signer_defaults_to_the_registrys_owner(target, chain):
    key, addr = _owned_by_eoa(chain)
    got = chain_link.ask("base-sepolia", AGENT, "", stable=STABLE)
    assert got["signer"].lower() == addr.lower() and got["tier"] == "owner"


def test_a_good_signature_is_checked_then_signed_by_the_agent_and_posted(target, chain, capsys, monkeypatch):
    key, addr = _owned_by_eoa(chain)
    issued, sig = _sign(key, addr)
    sent = {}

    def fake_request(method, url, *, agent_id="", json=None, **kw):
        sent.update(method=method, url=url, body=json)

        class R:
            status_code = 200
            def json(self_):
                return {"link": {"status": "claimed"}, "links": []}
        return R()
    monkeypatch.setattr(mcp_server, "_bearer_request", fake_request)
    monkeypatch.setattr(mcp_server, "_base", lambda: "http://relay.test")
    assert card.link_8004(target.name, "base-sepolia", str(AGENT), addr, sig, issued, yes=True) == 0
    out = capsys.readouterr().out
    assert "Checked here at block 123: owner" in out and "(testnet)" in out
    assert sent["url"] == f"http://relay.test/agents/{target.name}/links/erc8004"
    body = dict(sent["body"])
    agent_sig = body.pop("signature")
    payload = wire.signed_erc8004_link_payload(STABLE, f"eip155:84532:{ERC8004_REGISTRIES['eip155:84532'].lower()}",
                                               str(AGENT), addr, issued, sig, body["at"])
    assert identity.verify(payload, agent_sig, identity.get_public_key_hex(target.name))
    assert body["wallet_sig"] == sig and body["agent_id"] == str(AGENT)


def test_a_wrong_signature_posts_nothing(target, chain, capsys, monkeypatch):
    key, addr = _owned_by_eoa(chain)
    other, _ = keypair()
    issued, sig = _sign(other, addr)           # signed by a wallet that is not the signer
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda *a, **k: pytest.fail("posted"))
    assert card.link_8004(target.name, "base-sepolia", str(AGENT), addr, sig, issued, yes=True) == 1
    assert "not recorded" in capsys.readouterr().out


def test_a_wallet_that_does_not_control_it_is_not_recorded(target, chain, capsys, monkeypatch):
    chain["chain"].owners[AGENT] = keypair()[1]
    stranger, addr = keypair()
    issued, sig = _sign(stranger, addr)
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda *a, **k: pytest.fail("posted"))
    assert card.link_8004(target.name, "base-sepolia", str(AGENT), addr, sig, issued, yes=True) == 1
    assert "not-proven" in capsys.readouterr().out


def test_an_old_signature_is_refused(target, chain, capsys):
    key, addr = _owned_by_eoa(chain)
    issued, sig = _sign(key, addr, issued=int(time.time()) - 31 * 86400)
    assert card.link_8004(target.name, "base-sepolia", str(AGENT), addr, sig, issued, yes=True) == 1
    assert "older than 30 days" in capsys.readouterr().out


def test_recording_asks_first_and_there_is_no_asking_without_a_terminal(target, chain, capsys, monkeypatch):
    key, addr = _owned_by_eoa(chain)
    issued, sig = _sign(key, addr)
    monkeypatch.setattr(card._cli(), "_persons_terminal", lambda: False)
    assert card.link_8004(target.name, "base-sepolia", str(AGENT), addr, sig, issued) == 1
    assert "no terminal to ask in" in capsys.readouterr().out


@pytest.mark.parametrize("origin", ["relay", "rounds"])
def test_it_is_refused_while_an_unattended_turn_is_live(target, chain, capsys, origin):
    target.live = origin
    assert card.link_8004(target.name, "base-sepolia", str(AGENT)) == 1
    assert "person's to change" in capsys.readouterr().out
    assert card.unlink_8004(target.name, "base-sepolia", str(AGENT)) == 1
    assert capsys.readouterr().out.count("person's to change") == 1


def test_a_mainnet_identity_is_linkable_while_mainnet_money_is_locked(target, chain, capsys, monkeypatch):
    monkeypatch.delenv("EMPORIA_X402_MAINNET", raising=False)
    key, addr = _owned_by_eoa(chain)
    got = chain_link.ask("base", AGENT, addr, stable=STABLE)
    assert got["chain"] == "eip155:8453" and got["testnet"] is False and got["tier"] == "owner"
    assert "(testnet)" not in got["chain_label"]
    assert chain_link.ask("base-sepolia", AGENT, addr, stable=STABLE)["testnet"] is True
    assert card.link_8004(target.name, "base", str(AGENT), addr) == 0
    assert "owner" in capsys.readouterr().out


def test_an_unknown_chain_is_refused_by_name(target, chain, capsys):
    assert card.link_8004(target.name, "solana", str(AGENT)) == 1
    assert "no ERC-8004 registry" in capsys.readouterr().out
    assert card.link_8004(target.name, "base-sepolia", "abc") == 1
    assert card.link_8004(target.name, "", "") == 1


def test_a_dead_chain_says_so_and_changes_nothing(target, chain, capsys):
    chain["chain"].down = True
    assert card.link_8004(target.name, "base-sepolia", str(AGENT)) == 1
    assert "could not be read" in capsys.readouterr().out


def test_unlink_is_a_signed_removal_of_kind_chain_link(target, capsys, monkeypatch):
    seen = {}

    def fake_request(method, url, *, agent_id="", json=None, **kw):
        seen.update(url=url, body=json)

        class R:
            status_code = 200
            def json(self_):
                return {"ok": True}
        return R()
    monkeypatch.setattr(mcp_server, "_bearer_request", fake_request)
    monkeypatch.setattr(mcp_server, "_base", lambda: "http://relay.test")
    assert card.unlink_8004(target.name, "base-sepolia", str(AGENT)) == 0
    reg = f"eip155:84532:{ERC8004_REGISTRIES['eip155:84532'].lower()}"
    b = seen["body"]
    assert seen["url"] == "http://relay.test/delete" and b["kind"] == "chain-link"
    assert b["item"] == wire.chain_link_item(STABLE, reg, str(AGENT))
    assert identity.verify(wire.signed_delete_payload("chain-link", b["item"], STABLE, b["at"]), b["signature"],
                           identity.get_public_key_hex(target.name))
    assert "marker stays" in capsys.readouterr().out


@pytest.mark.parametrize("given,want", [("042", "42"), (42, "42"), (" 7 ", "7"), ("0", "0")])
def test_the_cli_writes_the_agent_id_in_canonical_form(given, want):
    assert chain_link._agent_id(given) == want


@pytest.mark.parametrize("bad", ["²", "+42", "-1", "4x", "", str(1 << 256)])
def test_the_cli_refuses_an_agent_id_that_is_not_a_number(bad):
    with pytest.raises(chain_link.LinkError):
        chain_link._agent_id(bad)


def test_the_mcp_delete_tool_refuses_a_chain_link(target, monkeypatch):
    sent = []
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda *a, **k: sent.append(a) or None)
    out = mcp_server.delete_item("chain-link", f"{STABLE}/eip155:84532:0x{'ab' * 20}/{AGENT}")
    assert out["success"] is False and "person's to change" in out["error"] and sent == []


def test_the_terminal_guard_blocks_link_and_unlink_for_an_agent():
    from hermes_plugin.emporia import key_guard as g
    for cmd in ("emporia agent link-8004 shop --chain base --agent-id 7 --signature 0x1 --signer 0x2 --issued-at 3 --yes",
                "emporia agent link-8004 shop --chain base --agent-id 7 --browser --yes",
                "emporia agent unlink-8004 shop --chain base --agent-id 7"):
        assert g.pre_tool_call("terminal", {"command": cmd})["action"] == "block", cmd


@pytest.mark.parametrize("kw", [{"signature": "0x" + "11" * 65, "signer": "0x" + "22" * 20, "issued_at": 1},
                                {"browser": True}])
def test_yes_does_not_stand_in_for_the_person_in_an_agent_turn(target, chain, capsys, monkeypatch, kw):
    monkeypatch.setenv("HERMES_SESSION_ID", "s1")
    assert card.link_8004(target.name, "base-sepolia", str(AGENT), yes=True, **kw) == 1
    assert "person's to change" in capsys.readouterr().out
    assert card.unlink_8004(target.name, "base-sepolia", str(AGENT)) == 1
    assert "person's to change" in capsys.readouterr().out


# -- --wait: poll the chain until it has caught up, then continue ----------------------------------------
def _no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(card.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def test_wait_polls_every_15_seconds_prints_progress_and_returns_the_ready_answer(monkeypatch, capsys):
    sleeps = _no_sleep(monkeypatch)
    answers = iter([{"success": False, "error": "that transaction is not mined yet"},
                    {"success": False, "error": "the chain's safe block is 5"}, {"success": True, "x": 1}])
    got = card._wait_for(lambda: next(answers), lambda o: not card._not_yet(o), "it", "resume me")
    assert got == {"success": True, "x": 1} and sleeps == [15.0, 15.0]
    assert capsys.readouterr().out.count("waiting for it") == 2


def test_wait_ends_on_an_answer_that_is_not_just_a_matter_of_time(monkeypatch):
    _no_sleep(monkeypatch)
    bad = {"success": False, "error": "the registry's owner of that token is not the one in the receipt"}
    assert card._wait_for(lambda: bad, lambda o: not card._not_yet(o), "it", "r") == bad


def test_wait_gives_up_after_30_minutes_and_prints_the_command(monkeypatch, capsys):
    clock = [0.0]
    monkeypatch.setattr(card.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(card.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + 600))
    nope = {"success": False, "error": "not mined yet"}
    assert card._wait_for(lambda: nope, lambda o: False, "it", "emporia agent resume --wait") is None
    out = capsys.readouterr().out
    assert "gave up" in out and "emporia agent resume --wait" in out and card.WAIT_LIMIT == 1800


def test_wait_stops_cleanly_on_ctrl_c_and_prints_the_resume_command(monkeypatch, capsys):
    def boom(_):
        raise KeyboardInterrupt
    monkeypatch.setattr(card.time, "sleep", boom)
    nope = {"success": False, "error": "not mined yet"}
    assert card._wait_for(lambda: nope, lambda o: False, "it", "emporia agent resume --wait") is None
    assert "stopped waiting" in capsys.readouterr().out


def test_link_wait_waits_for_the_agent_then_goes_on(target, chain, monkeypatch, capsys):
    sleeps = _no_sleep(monkeypatch)
    key, addr = _owned_by_eoa(chain)
    real, calls = card._chain_run, []

    def late(t, fn, *a, **k):
        calls.append(fn)
        if fn == "ask" and len(calls) < 3:
            return {"success": False, "error": "the agent does not exist in that registry"}
        return real(t, fn, *a, **k)
    monkeypatch.setattr(card, "_chain_run", late)
    assert card.link_8004(target.name, "base-sepolia", str(AGENT), wait=True) == 0
    assert sleeps == [15.0, 15.0] and "waiting for #" in capsys.readouterr().out


def test_register_wait_needs_a_transaction_or_the_browser(target, capsys):
    assert card.register_8004(target.name, "base-sepolia", wait=True) == 1
    assert "--wait goes with --tx" in capsys.readouterr().out
