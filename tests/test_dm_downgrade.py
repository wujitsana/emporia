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
from tests.test_emporia import _ASGIHttpxAdapter, _sdk_wired, prod_client  # noqa: F401 — the fixture


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
    assert any(c == "plain on purpose" for c in _stored(tid))


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
