"""The relay's ERC-8004 links: record, statuses, tiers, the limit, supersession, unlinking, lazy re-checks.
An in-memory chain stands behind the relay's reader; no network. Neutral synthetic fixtures."""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from emporia import erc8004, identity, operators, wire
from emporia import relay_server as rs
from emporia.agent_sdk import EmporiaAgent
from emporia.identity import get_public_key_hex
from emporia.networks import ERC8004_REGISTRIES
from emporia.routes import chain_links as cl
from tests.test_erc8004_verify import AGENT, C, FakeChain, bound_chain, keypair, needs_lib, sign

pytestmark = needs_lib
SEPOLIA, BASE = "eip155:84532", "eip155:8453"


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.delenv("EMPORIA_HOUSE_STABLE_IDS", raising=False)
    monkeypatch.delenv("EMPORIA_X402_MAINNET", raising=False)
    monkeypatch.setenv("EMPORIA_RPC_84532", "http://fake.invalid")
    monkeypatch.setenv("EMPORIA_RPC_8453", "http://fake.invalid")
    holder = {"chain": FakeChain()}

    def factory(caip2, urls):
        ch = holder["chain"]
        ch.caip2, ch.registry = caip2, ERC8004_REGISTRIES[caip2]
        return ch
    monkeypatch.setattr(cl, "reader_factory", factory)
    monkeypatch.setattr(cl, "spawn", lambda fn: fn())          # a re-read runs on the spot
    cl._inflight.clear(), cl._last_try.clear(), cl._reads.clear()
    with rs.DB_LOCK, rs.get_db() as conn:                       # the suite shares one relay database
        conn.execute("DELETE FROM agent_chain_links")
    transport = httpx.ASGITransport(app=rs.app)
    transport.chain = holder
    return transport


def agent(transport, tag="cl") -> EmporiaAgent:
    n = f"{tag}_{uuid.uuid4().hex[:6]}"
    a = EmporiaAgent(relay_url="http://testserver", agent_id=n, public_key_hex=get_public_key_hex(n),
                     operator_label=operators.new_label(), client_name="sdk")
    a._http = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    return a


def run(coro):
    return asyncio.run(coro)


async def stable_of(a):
    return (await a.get_agent_profile(a.agent_id))["stable_id"]


async def link(a, key, signer, *, agent_id=AGENT, caip2=SEPOLIA, issued=None, sig=None, as_agent=None, registry=None,
               name=None):
    """Post a link the way the CLI does: the wallet's signature, then the agent's own over the record."""
    issued = issued or int(time.time()) - 5
    registry = registry or ERC8004_REGISTRIES[caip2]
    stable = await stable_of(a)
    cid = int(caip2.split(":")[1])
    name = name or a.agent_id
    wallet = sig if sig is not None else sign(key, cid, registry, agent_id, signer, stable, issued=issued, name=name)
    reg = f"{caip2}:{registry.lower()}"
    at = datetime.now(UTC).isoformat()
    payload = wire.signed_erc8004_link_payload(stable, reg, str(agent_id), signer, issued, wallet, at, name)
    body = {"agent_registry": reg, "agent_id": str(agent_id), "signer": signer, "issued_at": issued,
            "agent_name": name, "wallet_sig": wallet, "at": at, "signature": identity.sign(payload, a.agent_id)}
    poster = as_agent or a
    return await poster._bearer_request("post", f"http://testserver/agents/{a.agent_id}/links/erc8004", json=body)


async def card(viewer, a):
    return (await viewer.get_agent_profile(a.agent_id))["links"]


def owned_by(relay, addr, agent_id=AGENT):
    relay.chain["chain"] = FakeChain()
    relay.chain["chain"].owners[agent_id] = addr
    return relay.chain["chain"]


def test_an_owner_links_and_the_card_says_checked_at_a_block(relay):
    key, addr = keypair()
    owned_by(relay, addr)

    async def go():
        a, b = agent(relay), agent(relay, "viewer")
        await a.register(), await b.register()
        r = await link(a, key, addr)
        return r, await card(b, a)

    r, links = run(go())
    assert r.status_code == 200, r.text
    (l,) = links
    assert l["site"] == "erc8004" and l["status"] == "checked" and l["tier"] == "owner" and l["declared"] is True
    assert l["agent_id"] == str(AGENT) and l["signer"] == addr and l["testnet"] is True
    assert l["checked"]["block_number"] == 123 and l["checked"]["block_hash"] == "0x" + "ab" * 32
    assert l["agent_registry"] == f"eip155:84532:{ERC8004_REGISTRIES[SEPOLIA].lower()}"
    # the card carries what the wallet signed, so a client can verify it itself
    assert isinstance(l["issued_at"], int) and bytes.fromhex(l["wallet_sig"].removeprefix("0x")) and len(l["wallet_sig"].removeprefix("0x")) == 130


def test_a_contract_owner_signs_by_erc1271(relay):
    key, _ = keypair()
    acct = C["smart_account"]
    ch = relay.chain["chain"]
    ch.codes.add(acct)
    ch.owners[AGENT] = acct
    ch.sig1271[acct.lower()] = "ok"

    async def go():
        a = agent(relay)
        await a.register()
        r = await link(a, key, acct)
        return r, await card(a, a)

    r, links = run(go())
    assert r.status_code == 200 and links[0]["tier"] == "owner" and links[0]["status"] == "checked"


@pytest.mark.parametrize("make,tier", [
    (lambda addr: bound_chain(erc8004.STD_ERC721, addr), "bound-owner"),
    (lambda addr: bound_chain(erc8004.STD_ERC721, addr, meta=False), "controller"),
])
def test_the_bound_and_controller_tiers(relay, make, tier):
    key, addr = keypair()
    relay.chain["chain"] = make(addr)

    async def go():
        a = agent(relay)
        await a.register()
        r = await link(a, key, addr)
        return r, await card(a, a)

    r, links = run(go())
    assert r.status_code == 200 and links[0]["tier"] == tier


def test_a_lying_owning_contract_is_capped_at_the_controller_tier(relay):
    key, addr = keypair()
    ch = bound_chain(erc8004.STD_ERC721, addr)
    ch.erc721[C["bound_token"].lower()] = {7: keypair()[1]}            # the bound token is held by someone else
    relay.chain["chain"] = ch

    async def go():
        a = agent(relay)
        await a.register()
        await link(a, key, addr)
        return await card(a, a)

    assert run(go())[0]["tier"] == "controller"


def test_a_wallet_that_controls_nothing_is_refused_and_nothing_is_recorded(relay):
    key, addr = keypair()
    owned_by(relay, keypair()[1])

    async def go():
        a = agent(relay)
        await a.register()
        return await link(a, key, addr), await card(a, a)

    r, links = run(go())
    assert r.status_code == 409 and "does not own" in r.text and links == []


def test_a_wrong_wallet_signature_is_refused(relay):
    key, addr = keypair()
    owned_by(relay, addr)

    async def go():
        a = agent(relay)
        await a.register()
        return await link(a, key, addr, sig=sign(keypair()[0], 84532, ERC8004_REGISTRIES[SEPOLIA], AGENT, addr,
                                                 await stable_of(a), issued=int(time.time()) - 5, name=a.agent_id))

    assert run(go()).status_code in (400, 409)


def test_stale_after_a_transfer_and_checked_again_when_it_comes_back(relay):
    key, addr = keypair()
    ch = owned_by(relay, addr)

    async def go():
        a = agent(relay)
        await a.register()
        await link(a, key, addr)
        return a, await stable_of(a)

    a, stable = run(go())
    k = (stable, f"{SEPOLIA}:{ERC8004_REGISTRIES[SEPOLIA].lower()}", str(AGENT))
    first = run(card(a, a))[0]["checked"]
    ch.owners[AGENT] = keypair()[1]                                    # sold
    ch.block_number = 130
    assert cl.recheck_link(k) == "stale"
    (l,) = run(card(a, a))
    assert l["status"] == "stale" and l["checked"] == first, "the record stays, with the last good read"
    ch.owners[AGENT] = addr
    ch.block_number = 140
    assert cl.recheck_link(k) == "checked"
    assert run(card(a, a))[0]["checked"]["block_number"] == 140


def test_an_rpc_failure_changes_nothing(relay):
    key, addr = keypair()
    ch = owned_by(relay, addr)

    async def go():
        a = agent(relay)
        await a.register()
        await link(a, key, addr)
        return a, await stable_of(a)

    a, stable = run(go())
    before = run(card(a, a))
    ch.down = True
    k = (stable, f"{SEPOLIA}:{ERC8004_REGISTRIES[SEPOLIA].lower()}", str(AGENT))
    assert cl.recheck_link(k) == "unknown"
    assert run(card(a, a)) == before


def test_a_relay_without_an_rpc_records_a_claim_and_takes_an_ordinary_account_only(relay, monkeypatch):
    monkeypatch.delenv("EMPORIA_RPC_84532")
    key, addr = keypair()
    owned_by(relay, addr)

    async def go():
        a = agent(relay)
        await a.register()
        ok = await link(a, key, addr)
        other = await link(a, key, C["smart_account"], agent_id=43)      # a contract wallet: only a read could settle it
        return ok, other, await card(a, a)

    ok, other, links = run(go())
    assert ok.status_code == 200 and links[0]["status"] == "claimed" and links[0]["checked"] is None
    assert other.status_code == 400 and "no RPC" in other.text and len(links) == 1


def test_an_agent_links_at_most_three(relay):
    key, addr = keypair()

    async def go():
        a = agent(relay)
        await a.register()
        codes = []
        for n in (1, 2, 3, 4):
            owned_by(relay, addr, agent_id=n)
            codes.append((await link(a, key, addr, agent_id=n)).status_code)
        return codes, await card(a, a)

    codes, links = run(go())
    assert codes == [200, 200, 200, 409] and len(links) == 3


def test_the_newest_link_takes_over_and_the_older_one_is_superseded(relay):
    key, addr = keypair()
    owned_by(relay, addr)

    async def go():
        a, b = agent(relay, "old"), agent(relay, "new")
        await a.register(), await b.register()
        await link(a, key, addr, issued=int(time.time()) - 100)
        await link(b, key, addr, issued=int(time.time()) - 5)
        return await card(a, a), await card(b, b)

    old, new = run(go())
    assert old[0]["status"] == "superseded" and new[0]["status"] == "checked"


def test_an_older_link_does_not_displace_a_newer_checked_one(relay):
    key, addr = keypair()
    owned_by(relay, addr)

    async def go():
        a, b = agent(relay, "first"), agent(relay, "second")
        await a.register(), await b.register()
        await link(a, key, addr, issued=int(time.time()) - 5)
        r = await link(b, key, addr, issued=int(time.time()) - 500)
        return r, await card(a, a), await card(b, b)

    r, first, second = run(go())
    assert r.status_code == 200 and first[0]["status"] == "checked" and second[0]["status"] == "superseded"


def test_unlinking_keeps_the_marker_and_the_link_leaves_the_card(relay):
    key, addr = keypair()
    owned_by(relay, addr)

    async def go():
        a = agent(relay)
        await a.register()
        await link(a, key, addr)
        stable = await stable_of(a)
        item = wire.chain_link_item(stable, f"{SEPOLIA}:{ERC8004_REGISTRIES[SEPOLIA].lower()}", str(AGENT))
        gone = await a.delete_item("chain-link", item)
        again = await a.delete_item("chain-link", item)
        return gone, again, await card(a, a), stable, item

    gone, again, links, stable, item = run(go())
    assert gone["ok"] and gone["removed"]["role"] == "author" and links == []
    assert again.get("already")
    from emporia import deletion
    with rs.DB_LOCK, rs.get_db() as conn:
        marker = deletion.removal_of(conn, "chain-link", item)
        kept = conn.execute("SELECT status FROM agent_chain_links WHERE stable_id = ?", (stable,)).fetchone()
    assert marker and marker["role"] == "author" and marker["stable_id"] == stable
    assert kept["status"] == "removed", "the signed record stays; only the card forgets it"


def test_another_agent_cannot_unlink_yours(relay):
    key, addr = keypair()
    owned_by(relay, addr)

    async def go():
        a, b = agent(relay), agent(relay, "other")
        await a.register(), await b.register()
        await link(a, key, addr)
        item = wire.chain_link_item(await stable_of(a), f"{SEPOLIA}:{ERC8004_REGISTRIES[SEPOLIA].lower()}", str(AGENT))
        try:
            await b.delete_item("chain-link", item)
            return None
        except httpx.HTTPStatusError as e:
            return e.response.status_code, await card(a, a)

    code, links = run(go())
    assert code == 403 and links[0]["status"] == "checked"


def test_another_agent_cannot_post_a_link_for_you(relay):
    key, addr = keypair()
    owned_by(relay, addr)

    async def go():
        a, b = agent(relay), agent(relay, "mallory")
        await a.register(), await b.register()
        r = await link(a, key, addr, as_agent=b)
        return r, await card(a, a)

    r, links = run(go())
    assert r.status_code == 403 and links == []


def test_a_statement_that_names_another_agent_is_refused(relay):
    """Fable delta review: the owner read "... to the Emporia agent alice"; mallory must not record it on her card,
    even with her own stable id in the signed key field."""
    key, addr = keypair()
    owned_by(relay, addr)

    async def go():
        m, v = agent(relay, "mallory"), agent(relay, "viewer")
        await m.register(), await v.register()
        r = await link(m, key, addr, name="alice")
        return r, await card(v, m)

    r, links = run(go())
    assert r.status_code == 400 and "names the Emporia agent 'alice'" in r.text and links == []


def test_a_mainnet_link_is_allowed_while_mainnet_money_is_locked(relay):
    from emporia import networks
    assert not networks.mainnet_unlocked()
    key, addr = keypair()
    owned_by(relay, addr)

    async def go():
        a = agent(relay)
        await a.register()
        r = await link(a, key, addr, caip2=BASE)
        t = await link(a, key, addr, caip2=SEPOLIA)
        return r, t, await card(a, a)

    r, t, links = run(go())
    assert r.status_code == 200 and t.status_code == 200
    by_chain = {l["agent_registry"].split(":")[1]: l for l in links}
    assert by_chain["8453"]["testnet"] is False and by_chain["8453"]["status"] == "checked"
    assert by_chain["84532"]["testnet"] is True


@pytest.mark.parametrize("what", ["other registry", "unknown chain"])
def test_only_canonical_registries_on_known_chains(relay, what):
    key, addr = keypair()
    owned_by(relay, addr)

    async def go():
        a = agent(relay)
        await a.register()
        stable = await stable_of(a)
        issued = int(time.time()) - 5
        reg = (f"{SEPOLIA}:{C['other_registry'].lower()}" if what == "other registry"
               else f"eip155:1:{ERC8004_REGISTRIES[SEPOLIA].lower()}")
        at = datetime.now(UTC).isoformat()
        body = {"agent_registry": reg, "agent_id": "42", "signer": addr, "issued_at": issued, "wallet_sig": "0x" + "11" * 65,
                "agent_name": a.agent_id, "at": at}
        body["signature"] = identity.sign(wire.signed_erc8004_link_payload(stable, reg, "42", addr, issued,
                                                                           body["wallet_sig"], at, a.agent_id), a.agent_id)
        return await a._bearer_request("post", f"http://testserver/agents/{a.agent_id}/links/erc8004", json=body)

    r = run(go())
    assert r.status_code == 400 and ("canonical" in r.text or "no ERC-8004 registry" in r.text)


def test_an_old_wallet_signature_is_refused(relay):
    key, addr = keypair()
    owned_by(relay, addr)

    async def go():
        a = agent(relay)
        await a.register()
        return await link(a, key, addr, issued=int(time.time()) - 31 * 86400)

    r = run(go())
    assert r.status_code == 400 and "older than 30 days" in r.text


def test_a_profile_view_re_reads_a_link_older_than_six_hours_once(relay):
    key, addr = keypair()
    ch = owned_by(relay, addr)

    async def go():
        a, b = agent(relay), agent(relay, "viewer")
        await a.register(), await b.register()
        await link(a, key, addr)
        return a, b, await stable_of(a)

    a, b, stable = run(go())
    old = datetime.fromtimestamp(time.time() - 7 * 3600, UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE agent_chain_links SET attempted_at = ?, checked_at = ? WHERE stable_id = ?", (old, old, stable))
    ch.block_number = 999
    run(card(b, a))                                                    # the view starts the re-read
    assert len(cl._reads) == 1, "the view started one read"
    assert run(card(b, a))[0]["checked"]["block_number"] == 123, "an unchanged re-read keeps the last good block"
    with rs.DB_LOCK, rs.get_db() as conn:
        assert conn.execute("SELECT attempted_at FROM agent_chain_links WHERE stable_id = ?", (stable,)).fetchone()[0] > old
    reads = len(cl._reads)
    run(card(b, a))                                                    # fresh now: no second read
    assert len(cl._reads) == reads


def test_re_reads_are_one_in_flight_per_link_and_rate_limited(relay, monkeypatch):
    key, addr = keypair()
    owned_by(relay, addr)
    started = []
    monkeypatch.setattr(cl, "spawn", lambda fn: started.append(fn))     # held: nothing runs, so the link stays in flight

    async def go():
        a = agent(relay)
        await a.register()
        await link(a, key, addr)
        return await stable_of(a)

    stable = run(go())
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE agent_chain_links SET attempted_at = '2020-01-01T00:00:00+00:00', "
                     "checked_at = '2020-01-01T00:00:00+00:00' WHERE stable_id = ?", (stable,))
    assert len(cl.schedule_rechecks([stable], now=1000.0)) == 1
    assert cl.schedule_rechecks([stable], now=1001.0) == [], "one read in flight per link"
    cl._inflight.clear()
    assert cl.schedule_rechecks([stable], now=1002.0) == [], "and a pause before it is tried again"
    assert len(cl.schedule_rechecks([stable], now=1000.0 + cl.RETRY_AFTER + 1)) == 1
    for _i in range(cl.READS_PER_MINUTE):
        cl._reads.append(5000.0)
    cl._inflight.clear()
    assert cl.schedule_rechecks([stable], now=5001.0) == [], "all re-reads share a budget"


def test_the_relay_stores_no_image_and_no_registration_text():
    with rs.DB_LOCK, rs.get_db() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_chain_links)")}
    # `agent_name` is the Emporia handle the wallet's statement names (1-80 printable characters, the agent's own
    # word): the relay keeps it only to rebuild the signed digest. It is not a registration name.
    assert not {c for c in cols if any(w in c for w in ("image", "uri", "name", "description", "text", "url"))} - {"agent_name"}


def _logged(stable):
    """The transparency-log entries of this agent's link, after folding the outbox in."""
    from emporia import translog
    with rs.DB_LOCK, rs.get_db() as conn:
        while translog.drain(conn):
            pass
        ident = "/".join((stable, f"{SEPOLIA}:{ERC8004_REGISTRIES[SEPOLIA].lower()}", str(AGENT)))
        return translog.entries(conn, ref=f"agent_chain_link:{ident}")


def test_a_recheck_writes_a_log_entry_only_when_status_tier_or_owner_change(relay):
    from emporia import translog
    key, addr = keypair()
    ch = owned_by(relay, addr)

    async def go():
        a = agent(relay)
        await a.register()
        await link(a, key, addr)
        return a, await stable_of(a)

    a, stable = run(go())
    k = (stable, f"{SEPOLIA}:{ERC8004_REGISTRIES[SEPOLIA].lower()}", str(AGENT))
    if not translog.enabled():
        pytest.skip("the transparency log is off")
    base = len(_logged(stable))
    assert base >= 1
    ch.block_number = 130
    assert cl.recheck_link(k) == "checked"
    assert len(_logged(stable)) == base, "an unchanged re-read adds no entry"
    assert run(card(a, a))[0]["checked"]["block_number"] == 123, "the block of the last good read stays"
    ch.owners[AGENT] = keypair()[1]
    ch.block_number = 140
    assert cl.recheck_link(k) == "stale"
    assert len(_logged(stable)) == base + 1
    assert cl.recheck_link(k) == "stale"
    assert len(_logged(stable)) == base + 1, "a second stale read changes nothing"
    ch.owners[AGENT] = addr
    ch.block_number = 150
    assert cl.recheck_link(k) == "checked"
    assert len(_logged(stable)) == base + 2
    assert run(card(a, a))[0]["checked"]["block_number"] == 150


# -- an unproven claim never knocks out a real link -----------------------------------------------------
def test_an_unproven_claim_does_not_supersede_a_claimed_link_of_another_agent(relay, monkeypatch):
    monkeypatch.delenv("EMPORIA_RPC_84532")
    key, addr = keypair()
    evil, evil_addr = keypair()
    owned_by(relay, addr)

    async def go():
        a, b = agent(relay), agent(relay, "evil")
        await a.register(), await b.register()
        r1 = await link(a, key, addr, issued=int(time.time()) - 100)
        r2 = await link(b, evil, evil_addr, issued=int(time.time()) - 5)
        return r1, r2, await card(a, a), await card(b, b)

    r1, r2, victim, attacker = run(go())
    assert r1.status_code == 200 and victim[0]["status"] == "claimed", "the first claim stays"
    assert r2.status_code == 409 and "already claimed by" in r2.text and "checked link takes it" in r2.text
    assert attacker == []


def test_a_checked_link_still_takes_a_claimed_one_over(relay, monkeypatch):
    key, addr = keypair()
    owned_by(relay, addr)
    monkeypatch.delenv("EMPORIA_RPC_84532")

    async def go():
        a, b = agent(relay, "claimer"), agent(relay, "prover")
        await a.register(), await b.register()
        await link(a, key, addr, issued=int(time.time()) - 100)
        monkeypatch.setenv("EMPORIA_RPC_84532", "http://fake.invalid")
        await link(b, key, addr, issued=int(time.time()) - 5)
        return await card(a, a), await card(b, b)

    old, new = run(go())
    assert old[0]["status"] == "superseded" and new[0]["status"] == "checked"


def test_a_claim_is_stored_beside_nothing_when_a_checked_link_stands(relay, monkeypatch):
    key, addr = keypair()
    owned_by(relay, addr)

    async def go():
        a, b = agent(relay, "first"), agent(relay, "second")
        await a.register(), await b.register()
        await link(a, key, addr, issued=int(time.time()) - 100)
        ch = relay.chain["chain"]
        ch.down = True                                  # an RPC outage: the second is only claimed
        r = await link(b, key, addr, issued=int(time.time()) - 5)
        return r, await card(a, a), await card(b, b)

    r, first, second = run(go())
    assert r.status_code == 200 and first[0]["status"] == "checked" and second[0]["status"] == "superseded"


def test_a_relay_that_cannot_check_a_signature_refuses_the_claim(relay, monkeypatch):
    monkeypatch.delenv("EMPORIA_RPC_84532")
    monkeypatch.setattr(erc8004, "available", lambda: False)
    key, addr = keypair()

    async def go():
        a = agent(relay)
        await a.register()
        return await link(a, key, addr, sig="0x" + "11" * 65), await card(a, a)

    r, links = run(go())
    assert r.status_code == 400 and "cannot check a wallet signature" in r.text and links == []


def test_a_leading_zero_agent_id_is_refused_by_the_relay(relay):
    key, addr = keypair()
    owned_by(relay, addr)

    async def go():
        a = agent(relay)
        await a.register()
        stable = await stable_of(a)
        issued = int(time.time()) - 5
        reg = f"{SEPOLIA}:{ERC8004_REGISTRIES[SEPOLIA].lower()}"
        at = datetime.now(UTC).isoformat()
        wsig = sign(key, 84532, ERC8004_REGISTRIES[SEPOLIA], AGENT, addr, stable, issued=issued)
        body = {"agent_registry": reg, "agent_id": "0%d" % AGENT, "signer": addr, "issued_at": issued,
                "agent_name": a.agent_id, "wallet_sig": wsig, "at": at}
        payload = {"type": wire.ERC8004_LINK_TYPE, "stable_id": stable, "agent_registry": reg,
                   "agent_id": body["agent_id"], "signer": addr, "issued_at": issued, "agent_name": a.agent_id,
                   "wallet_sig": wsig, "at": at}
        body["signature"] = identity.sign(payload, a.agent_id)
        return await a._bearer_request("post", f"http://testserver/agents/{a.agent_id}/links/erc8004", json=body), \
            await card(a, a)

    r, links = run(go())
    assert r.status_code == 400 and "canonical" in r.text and links == []


def test_a_link_naming_another_agent_than_the_wallet_signed_is_refused(relay):
    key, addr = keypair()
    owned_by(relay, addr)

    async def go():
        a = agent(relay)
        await a.register()
        stable = await stable_of(a)
        issued = int(time.time()) - 5
        wallet = sign(key, 84532, ERC8004_REGISTRIES[SEPOLIA], AGENT, addr, stable, issued=issued, name="someone-else")
        reg = f"{SEPOLIA}:{ERC8004_REGISTRIES[SEPOLIA].lower()}"
        at = datetime.now(UTC).isoformat()
        body = {"agent_registry": reg, "agent_id": str(AGENT), "signer": addr, "issued_at": issued,
                "agent_name": a.agent_id, "wallet_sig": wallet, "at": at}
        body["signature"] = identity.sign(wire.signed_erc8004_link_payload(
            stable, reg, str(AGENT), addr, issued, wallet, at, a.agent_id), a.agent_id)
        return await a._bearer_request("post", f"http://testserver/agents/{a.agent_id}/links/erc8004", json=body)

    assert run(go()).status_code in (400, 409)
