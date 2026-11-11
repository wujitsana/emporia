"""S-9 / S-10 (signer audit): the relay's word is never enough to send plaintext.

S-9a: a DM thread the relay calls plaintext is refused unless the caller asked for plaintext.
S-9b: a room's `encrypted` flag is checked against its creator's signed creation, and the
      room id is bound to the creator first verified (a relay cannot re-parent it).
S-10: encryption pins from a visited relay do not touch the home peer's pin.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from emporia import identity
from emporia.relay_server import get_db
from tests import test_emporia
from tests.test_emporia import _ASGIHttpxAdapter, _sdk_wired

prod_client = test_emporia.prod_client


def _stored(thread_id: str) -> list[str]:
    with get_db() as conn:
        return [r["content"] for r in conn.execute("SELECT content FROM dm_messages WHERE thread_id = ?",
                                                   (thread_id,))]


# -- S-9a: DMs --------------------------------------------------------------------------

def test_mcp_refuses_a_plaintext_thread_the_relay_offers_even_at_first_contact(prod_client, monkeypatch):
    from emporia import mcp_server
    adapter = _ASGIHttpxAdapter(prod_client, mcp_server.RELAY_URL)
    with patch.object(mcp_server, "httpx", adapter):
        mcp_server.register_agent(agent_id="s9a_a")
        mcp_server.register_agent(agent_id="s9a_b")
        assert "s9a_b" not in identity.load_peer_encryption_keys()      # nothing pinned: first contact
        real, sent = mcp_server._bearer_request, []

        def hostile(method, url, **kw):
            if url.endswith("/dm/start"):
                body = {**real(method, url, **kw).json(), "encrypted": False}
                return SimpleNamespace(status_code=200, json=lambda: body, text="")
            if url.endswith("/send"):
                sent.append(kw.get("json"))
            return real(method, url, **kw)
        monkeypatch.setattr(mcp_server, "_bearer_request", hostile)
        out = mcp_server.send_dm("s9a_b", "the vault code is 5521", from_agent_id="s9a_a")
        assert not out.get("success"), out
        assert "encryption key" in out["error"], out
        assert "encrypted=False" not in out["error"], "the refusal must not teach the downgrade (FR-3)"
        assert not sent, "nothing reached the relay"


def test_mcp_sends_no_plaintext_dm_in_a_turn_nobody_at_the_keyboard_started(prod_client, monkeypatch):
    """Final review FR-3: encrypted=False is the person's choice; in a relay-started (or unknown) turn a plaintext
    thread the relay offers is refused even when the call asks for plaintext."""
    from emporia import mcp_server
    adapter = _ASGIHttpxAdapter(prod_client, mcp_server.RELAY_URL)
    with patch.object(mcp_server, "httpx", adapter):
        mcp_server.register_agent(agent_id="fr3_a")
        mcp_server.register_agent(agent_id="fr3_b")
        real, sent = mcp_server._bearer_request, []

        def hostile(method, url, **kw):
            if url.endswith("/dm/start"):
                body = {**real(method, url, **kw).json(), "encrypted": False}
                return SimpleNamespace(status_code=200, json=lambda: body, text="")
            if url.endswith("/send"):
                sent.append(kw.get("json"))
            return real(method, url, **kw)
        monkeypatch.setattr(mcp_server, "_bearer_request", hostile)
        monkeypatch.setenv("EMPORIA_UNTRACKED_TURNS", "")       # real turn tracking: no live person turn
        out = mcp_server.send_dm("fr3_b", "the vault code is 5521", from_agent_id="fr3_a", encrypted=False)
        assert not out.get("success"), out
        assert "your person started" in out["error"], out
        assert not sent, "nothing reached the relay"


def test_sdk_refuses_a_plaintext_thread_it_did_not_ask_for(prod_client):
    async def scenario():
        alice, bob = _sdk_wired("s9a_sdk_a"), _sdk_wired("s9a_sdk_b")
        try:
            await alice.register()
            await bob.register()
            tid = (await alice.start_dm("s9a_sdk_b"))["thread_id"]
            real = alice._bearer_request

            async def lying(method, url, **kw):
                r = await real(method, url, **kw)
                if method == "get" and "/dm/" in url and "/messages" not in url:
                    body = {**r.json(), "encrypted": False}
                    return SimpleNamespace(status_code=200, json=lambda: body, raise_for_status=lambda: None)
                return r
            alice._bearer_request = lying
            with pytest.raises(identity.PlaintextRefused, match="your person"):
                await alice.send_dm(tid, "the vault code is 3318")
            return tid
        finally:
            await alice.__aexit__()
            await bob.__aexit__()
    tid = asyncio.run(scenario())
    assert not any("3318" in c for c in _stored(tid))


# -- S-9b: rooms ------------------------------------------------------------------------

def _make_room(mcp_server, creator, encrypted):
    made = mcp_server.create_room("s9b room", creator_id=creator, encrypted=encrypted)
    assert made.get("success"), made
    return made["room"]["room_id"]


def _serving_room(mcp_server, monkeypatch, change):
    """A hostile relay's `GET /rooms/{id}`: the real answer, changed by `change(body)`."""
    real, sent = mcp_server._bearer_request, []

    def hostile(method, url, **kw):
        r = real(method, url, **kw)
        if method == "get" and "/rooms/" in url and url.split("/rooms/")[1].count("/") == 0:
            body = change(dict(r.json()))
            return SimpleNamespace(status_code=200, json=lambda: body, text="")
        if url.endswith("/message"):
            sent.append(kw.get("json"))
        return r
    monkeypatch.setattr(mcp_server, "_bearer_request", hostile)
    return sent


def test_mcp_will_not_post_plaintext_into_a_room_its_creator_signed_encrypted(prod_client, monkeypatch):
    from emporia import mcp_server
    adapter = _ASGIHttpxAdapter(prod_client, mcp_server.RELAY_URL)
    with patch.object(mcp_server, "httpx", adapter):
        mcp_server.register_agent(agent_id="s9b_a")
        rid = _make_room(mcp_server, "s9b_a", True)
        sent = _serving_room(mcp_server, monkeypatch, lambda b: {**b, "encrypted": False})
        out = mcp_server.send_room_message(rid, "the vault code is 6107", sender_id="s9b_a")
        assert not out.get("success") and "did not sign" in out["error"], out
        assert not sent, "nothing reached the relay"


def test_mcp_will_not_post_plaintext_when_the_creation_cannot_be_verified(prod_client, monkeypatch):
    from emporia import mcp_server
    adapter = _ASGIHttpxAdapter(prod_client, mcp_server.RELAY_URL)
    with patch.object(mcp_server, "httpx", adapter):
        mcp_server.register_agent(agent_id="s9b_c")
        rid = _make_room(mcp_server, "s9b_c", True)
        sent = _serving_room(mcp_server, monkeypatch,
                             lambda b: {**b, "encrypted": False, "signed_acts": []})
        out = mcp_server.send_room_message(rid, "the vault code is 7208", sender_id="s9b_c")
        assert not out.get("success") and "only the relay's word" in out["error"], out
        assert not sent


def test_a_plaintext_room_its_creator_signed_plaintext_still_works(prod_client):
    from emporia import mcp_server
    adapter = _ASGIHttpxAdapter(prod_client, mcp_server.RELAY_URL)
    with patch.object(mcp_server, "httpx", adapter):
        mcp_server.register_agent(agent_id="s9b_d")
        rid = _make_room(mcp_server, "s9b_d", False)
        out = mcp_server.send_room_message(rid, "hello room", sender_id="s9b_d")
        assert out.get("success"), out
        rec = json.loads((identity.KEY_DIR / "room_creators.json").read_text())
        assert any(v["encrypted"] is False and v["creator"].startswith("s9b_d@") for v in rec.values())
        assert oct((identity.KEY_DIR / "room_creators.json").stat().st_mode & 0o777) == oct(0o600)


def test_mcp_refuses_a_room_re_parented_under_another_creator(prod_client, monkeypatch):
    """The relay serves the room id with a creator of its own (whose own signed creation, here
    a second real room's, verifies): the client recorded who made it and refuses."""
    from emporia import mcp_server
    adapter = _ASGIHttpxAdapter(prod_client, mcp_server.RELAY_URL)
    with patch.object(mcp_server, "httpx", adapter):
        mcp_server.register_agent(agent_id="s9b_real")
        mcp_server.register_agent(agent_id="s9b_evil")
        rid = _make_room(mcp_server, "s9b_real", True)
        evil_rid = _make_room(mcp_server, "s9b_evil", True)
        first = mcp_server.send_room_message(rid, "first, sealed", sender_id="s9b_real")
        assert first.get("success"), first
        real_get = mcp_server._bearer_request
        evil = real_get("get", f"{mcp_server._base()}/rooms/{evil_rid}", agent_id="s9b_real", timeout=10).json()
        sent = _serving_room(mcp_server, monkeypatch, lambda b: {**evil, "room_id": rid})
        out = mcp_server.send_room_message(rid, "the vault code is 8814", sender_id="s9b_real")
        assert not out.get("success") and "first verified here" in out["error"], out
        assert not sent


# -- S-10: encryption pins per relay -----------------------------------------------------

def test_a_namesake_on_a_visited_relay_does_not_move_the_home_peers_pin(tmp_path, monkeypatch):
    import os
    from datetime import UTC, datetime, timedelta

    from cryptography.hazmat.primitives.asymmetric import ed25519

    from emporia import wire
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    home, visited = "https://home.s10.example", "https://visited.s10.example"
    monkeypatch.setenv("EMPORIA_RELAY_URL", home)

    def agent():
        k = ed25519.Ed25519PrivateKey.generate()
        return k, k.public_key().public_bytes_raw().hex()

    def bound(priv, at):
        enc = os.urandom(32).hex()
        return enc, identity.sign_payload(priv, wire.signed_encryption_key_payload("bob", enc, at))

    t_home = datetime.now(UTC).isoformat()
    t_visit = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    home_priv, home_pub = agent()
    visit_priv, visit_pub = agent()
    e_home, sig_home = bound(home_priv, t_home)
    assert identity.trust_peer_encryption_key("bob", e_home, sig_home, home_pub, home, at=t_home) == e_home
    # a namesake on the visited relay, with a newer binding of its own
    e_visit, sig_visit = bound(visit_priv, t_visit)
    assert identity.trust_peer_encryption_key("bob", e_visit, sig_visit, visit_pub, visited, at=t_visit) == e_visit
    pins = identity.load_peer_encryption_keys()
    assert pins["bob"] == e_home                         # the home peer's pin is untouched
    assert pins["bob@visited.s10.example"] == e_visit
    # and the home peer still seals to the home key
    assert identity.trust_peer_encryption_key("bob", e_home, sig_home, home_pub, home, at=t_home) == e_home


def test_an_old_bare_handle_pin_is_still_read_as_the_home_relays(tmp_path, monkeypatch):
    import os

    from cryptography.hazmat.primitives.asymmetric import ed25519

    from emporia import wire
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    monkeypatch.setenv("EMPORIA_RELAY_URL", "https://home.s10b.example")
    k = ed25519.Ed25519PrivateKey.generate()
    pub = k.public_key().public_bytes_raw().hex()
    enc = os.urandom(32).hex()
    sig = identity.sign_payload(k, wire.signed_encryption_key_payload("carol", enc, ""))
    identity.pin_peer_encryption_key("carol", enc)                       # an older file: bare handle
    assert identity.trust_peer_encryption_key("carol", enc, sig, pub, "https://home.s10b.example") == enc
    other = os.urandom(32).hex()
    other_sig = identity.sign_payload(k, wire.signed_encryption_key_payload("carol", other, ""))
    with pytest.raises(identity.PeerKeyMismatch):                        # it still guards the home peer
        identity.trust_peer_encryption_key("carol", other, other_sig, pub, "https://home.s10b.example")
