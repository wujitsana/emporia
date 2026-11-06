"""P298 (security audit, 11-05): a relay cannot turn an agent's DMs back into plaintext.

Whether a DM is sealed was the relay's word: both clients sealed only when `/dm/start`
(MCP) or `GET /dm/{id}` (SDK) said the thread was encrypted, so a hostile relay — or
anyone who can write its database — answered `encrypted: false` (or cleared the
peer's published key) and read every message after. The threat model is that the
relay operator and anyone with relay or DB access cannot read DM content.

A client that has already sealed to a peer has that peer's encryption key pinned, so
it knows the peer can receive sealed messages: from then on a thread the relay calls
plaintext is refused, unless the agent asked for plaintext itself.
"""
import asyncio
from unittest.mock import patch

import pytest

from emporia import identity
from emporia.relay_server import DB_LOCK, get_db
from tests import test_emporia
from tests.test_emporia import _ASGIHttpxAdapter, _sdk_wired

prod_client = test_emporia.prod_client      # the fixture; bound, not imported, so no F811


def _strip_encryption(thread_id: str, peer: str) -> None:
    """What a hostile relay, or anyone with its database, does: the thread marked
    plaintext, and the peer's published key gone so the relay does not upgrade it again."""
    with DB_LOCK, get_db() as conn:
        conn.execute("UPDATE dm_threads SET encrypted = 0 WHERE thread_id = ?", (thread_id,))
        conn.execute("UPDATE authorized_agents SET encryption_public_key = '', encryption_key_signature = '' "
                     "WHERE agent_id = ?", (peer,))


def _stored(thread_id: str) -> list[str]:
    with get_db() as conn:
        return [r["content"] for r in conn.execute("SELECT content FROM dm_messages WHERE thread_id = ?",
                                                   (thread_id,))]


def test_mcp_will_not_send_in_the_clear_to_a_peer_it_has_sealed_to(prod_client):
    from emporia import mcp_server
    adapter = _ASGIHttpxAdapter(prod_client, mcp_server.RELAY_URL)
    with patch.object(mcp_server, "httpx", adapter):
        mcp_server.register_agent(agent_id="p298_dg_a")
        mcp_server.register_agent(agent_id="p298_dg_b")
        first = mcp_server.send_dm("p298_dg_b", "sealed, pins b's key", from_agent_id="p298_dg_a")
        assert first.get("success"), first
        tid = first["thread_id"]
        assert identity.load_peer_encryption_keys()["p298_dg_b"]

        _strip_encryption(tid, "p298_dg_b")
        secret = "the vault code is 4417"
        sent = mcp_server.send_dm("p298_dg_b", secret, from_agent_id="p298_dg_a")
        assert not sent.get("success") and "plaintext" in sent["error"], sent
        assert not any("4417" in c for c in _stored(tid)), "the relay never saw it"

        asked = mcp_server.send_dm("p298_dg_b", "plain on purpose", from_agent_id="p298_dg_a", encrypted=False)
        assert asked.get("success"), asked                    # the agent's own choice still works


def test_the_sdk_will_not_send_in_the_clear_to_a_peer_it_has_sealed_to(prod_client):
    secret = "the vault code is 9921"

    async def scenario():
        alice, bob = _sdk_wired("p298_sdk_a"), _sdk_wired("p298_sdk_b")
        try:
            await alice.register()
            await bob.register()
            tid = (await alice.start_dm("p298_sdk_b"))["thread_id"]
            await alice.send_dm(tid, "sealed, pins bob's key")
            _strip_encryption(tid, "p298_sdk_b")
            with pytest.raises(identity.PlaintextRefused):
                await alice.send_dm(tid, secret)
            await alice.send_dm(tid, "plain on purpose", plaintext_ok=True)
            return tid
        finally:
            await alice.__aexit__()
            await bob.__aexit__()

    tid = asyncio.run(scenario())
    assert not any("9921" in c for c in _stored(tid))
    assert any("plain on purpose" in c for c in _stored(tid))


def test_a_first_plaintext_thread_with_a_peer_never_sealed_to_still_works(prod_client):
    async def scenario():
        alice, bob = _sdk_wired("p298_pt_a"), _sdk_wired("p298_pt_b")
        try:
            await alice.register()
            await bob.register()
            tid = (await alice.start_dm("p298_pt_b", encrypted=False))["thread_id"]
            await alice.send_dm(tid, "hello in the clear")
        finally:
            await alice.__aexit__()
            await bob.__aexit__()
    asyncio.run(scenario())


def test_the_sdk_will_not_send_in_the_clear_when_it_asked_for_a_seal(prod_client):
    """Review pass (11-05): the SDK's counterpart. `start_dm(encrypted=True)` answered with a
    plaintext thread is refused, and that thread stays one this agent requires sealed, so a
    relay that calls it plaintext later — before any key is pinned — gets nothing."""
    async def scenario():
        alice, bob = _sdk_wired("rp_sdk_a"), _sdk_wired("rp_sdk_b")
        try:
            await alice.register()
            await bob.register()
            real = alice._bearer_request

            async def lying(method, url, **kw):
                r = await real(method, url, **kw)
                if url.endswith("/dm/start") or (method == "get" and "/dm/" in url and "/messages" not in url):
                    body = {**r.json(), "encrypted": False}
                    from types import SimpleNamespace
                    return SimpleNamespace(status_code=200, json=lambda: body, raise_for_status=lambda: None)
                return r
            alice._bearer_request = lying
            with pytest.raises(identity.PlaintextRefused):
                await alice.start_dm("rp_sdk_b", encrypted=True)
            alice._bearer_request = real
            tid = (await alice.start_dm("rp_sdk_b", encrypted=True))["thread_id"]
            alice._bearer_request = lying
            assert "rp_sdk_b" not in identity.load_peer_encryption_keys()
            with pytest.raises(identity.PlaintextRefused):
                await alice.send_dm(tid, "the vault code is 6620")
            return tid
        finally:
            await alice.__aexit__()
            await bob.__aexit__()

    tid = asyncio.run(scenario())
    assert not any("6620" in c for c in _stored(tid))


def _lying_about_dm_start(mcp_server, monkeypatch, fake: dict | None = None) -> list:
    """A hostile relay's `/dm/start`: whatever was asked, the thread is "not encrypted"
    (`fake`: the whole answer, for a thread it never made). Returns the bodies it was sent."""
    from types import SimpleNamespace
    real, sent = mcp_server._bearer_request, []

    def hostile(method, url, **kw):
        if url.endswith("/dm/start"):
            body = fake if fake is not None else {**real(method, url, **kw).json(), "encrypted": False}
            return SimpleNamespace(status_code=200, json=lambda: body, text="")
        if url.endswith("/send"):
            sent.append(kw.get("json"))
        return real(method, url, **kw)
    monkeypatch.setattr(mcp_server, "_bearer_request", hostile)
    return sent


def test_mcp_will_not_send_in_the_clear_when_it_asked_for_a_seal(prod_client, monkeypatch):
    """Review pass (11-05): `encrypted=True` is the agent requiring a seal. With a peer not
    yet pinned (first contact), a relay answering `encrypted: false` got the message in
    plaintext — the downgrade check covered only peers sealed to before."""
    from emporia import mcp_server
    adapter = _ASGIHttpxAdapter(prod_client, mcp_server.RELAY_URL)
    with patch.object(mcp_server, "httpx", adapter):
        mcp_server.register_agent(agent_id="rp_seal_a")
        mcp_server.register_agent(agent_id="rp_seal_b")
        assert "rp_seal_b" not in identity.load_peer_encryption_keys()
        sent = _lying_about_dm_start(mcp_server, monkeypatch)
        out = mcp_server.send_dm("rp_seal_b", "the vault code is 7731", from_agent_id="rp_seal_a",
                                 encrypted=True)
        assert not out.get("success") and "plaintext" in out["error"], out
        assert not sent, "nothing reached the relay"


def test_mcp_will_not_send_in_the_clear_to_another_relays_agent(prod_client, monkeypatch):
    """Review pass (11-05): a DM between relays is always sealed (the relay itself refuses a
    plaintext one), so a home relay calling such a thread plaintext is lying."""
    from emporia import mcp_server
    adapter = _ASGIHttpxAdapter(prod_client, mcp_server.RELAY_URL)
    with patch.object(mcp_server, "httpx", adapter):
        mcp_server.register_agent(agent_id="rp_fed_a")
        sent = _lying_about_dm_start(mcp_server, monkeypatch,
                                     fake={"thread_id": "dm_fake", "encrypted": False})
        out = mcp_server.send_dm("bob@relay-b.test", "the vault code is 8842", from_agent_id="rp_fed_a")
        assert not out.get("success") and "plaintext" in out["error"], out
        assert not sent, "nothing reached the relay"


# -- Review follow-up M3 (11-05): who a thread is with is the client's record, not the relay's --

def _relay_serving(agent, change):
    """A hostile relay's `GET /dm/{id}`: the real answer, changed by `change(body)`."""
    from types import SimpleNamespace
    real = agent._bearer_request

    async def hostile(method, url, **kw):
        r = await real(method, url, **kw)
        if method == "get" and "/dm/" in url and not url.endswith(("/messages", "/chain")):
            body = change(dict(r.json()))
            return SimpleNamespace(status_code=200, json=lambda: body, raise_for_status=lambda: None)
        return r
    agent._bearer_request = hostile


@pytest.mark.parametrize("how", ["renamed", "my_label"])
def test_the_sdk_will_not_take_the_threads_peer_from_the_relay(prod_client, how):
    """M3: P298's check asked whether the thread's *other participant*, as `GET /dm/{id}`
    named it, had a pinned key. A relay that renamed the peer to an id with no pin, or set
    `my_label` to the peer so it was filtered out as "me", called the thread plaintext and
    got the message in the clear. The peer is now the one `start_dm` asked for."""
    a, b = f"m3_{how}_a", f"m3_{how}_b"

    def change(t):
        if how == "renamed":
            return {**t, "encrypted": False, "agent_a": a, "agent_b": "m3_nobody_pinned"}
        return {**t, "encrypted": False, "my_label": b}

    async def scenario():
        alice, bob = _sdk_wired(a), _sdk_wired(b)
        try:
            await alice.register()
            await bob.register()
            tid = (await alice.start_dm(b))["thread_id"]
            await alice.send_dm(tid, "sealed, pins bob's key")
            _relay_serving(alice, change)
            with pytest.raises(identity.ThreadPeerChanged):
                await alice.send_dm(tid, "the vault code is 5150")
            return tid
        finally:
            await alice.__aexit__()
            await bob.__aexit__()

    tid = asyncio.run(scenario())
    assert not any("5150" in c for c in _stored(tid))


def test_a_thread_required_sealed_stays_sealed_in_a_new_process(prod_client):
    """M3: the review pass remembered `start_dm(encrypted=True)` threads in memory only, so
    a restarted agent (no key pinned yet) sent in plaintext when the relay said so."""
    async def scenario():
        alice, bob = _sdk_wired("m3_new_a"), _sdk_wired("m3_new_b")
        try:
            await alice.register()
            await bob.register()
            tid = (await alice.start_dm("m3_new_b", encrypted=True))["thread_id"]
        finally:
            await alice.__aexit__()
            await bob.__aexit__()
        again = _sdk_wired("m3_new_a")                     # the agent, restarted
        try:
            assert "m3_new_b" not in identity.load_peer_encryption_keys()
            _relay_serving(again, lambda t: {**t, "encrypted": False})
            with pytest.raises(identity.PlaintextRefused):
                await again.send_dm(tid, "the vault code is 3141")
        finally:
            await again.__aexit__()
        return tid

    tid = asyncio.run(scenario())
    assert not any("3141" in c for c in _stored(tid))


def test_the_sdk_refuses_a_thread_the_relay_says_is_with_someone_else(prod_client):
    from types import SimpleNamespace

    async def scenario():
        alice, bob = _sdk_wired("m3_ask_a"), _sdk_wired("m3_ask_b")
        try:
            await alice.register()
            await bob.register()
            real = alice._bearer_request

            async def other(method, url, **kw):
                r = await real(method, url, **kw)
                if url.endswith("/dm/start"):
                    body = {**r.json(), "agent_a": "m3_ask_a", "agent_b": "m3_ask_x"}
                    return SimpleNamespace(status_code=200, json=lambda: body, raise_for_status=lambda: None)
                return r
            alice._bearer_request = other
            with pytest.raises(identity.ThreadPeerChanged):
                await alice.start_dm("m3_ask_b")
        finally:
            await alice.__aexit__()
            await bob.__aexit__()
    asyncio.run(scenario())


def test_mcp_seals_only_to_the_agent_it_addressed(prod_client, monkeypatch):
    """M3, MCP side: `_dm_recipients` sealed to both participants `GET /dm/{id}` listed, so
    a relay naming an agent of its own as the peer got a copy it could open."""
    from types import SimpleNamespace

    from emporia import mcp_server
    adapter = _ASGIHttpxAdapter(prod_client, mcp_server.RELAY_URL)
    with patch.object(mcp_server, "httpx", adapter):
        for x in ("m3_mcp_a", "m3_mcp_b", "m3_mcp_relays_own"):
            mcp_server.register_agent(agent_id=x)
        real, sent = mcp_server._bearer_request, []

        def hostile(method, url, **kw):
            r = real(method, url, **kw)
            if method == "get" and "/dm/" in url and not url.endswith(("/messages", "/chain")):
                body = {**r.json(), "agent_a": "m3_mcp_a", "agent_b": "m3_mcp_relays_own"}
                return SimpleNamespace(status_code=200, json=lambda: body, text="", raise_for_status=lambda: None)
            if url.endswith("/send"):
                sent.append(kw.get("json"))
            return r
        monkeypatch.setattr(mcp_server, "_bearer_request", hostile)
        out = mcp_server.send_dm("m3_mcp_b", "for bob only", from_agent_id="m3_mcp_a")
        assert not out.get("success") and "m3_mcp_relays_own" in out["error"], out
        assert not sent, "nothing was sealed to the relay's agent"
