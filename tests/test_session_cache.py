"""A signed-in session is kept on disk between process starts, reused until shortly before it
expires, forgotten when refused, and kept private (0600, never printed)."""

import asyncio
import json
import stat

import pytest

from emporia import identity, session_cache

RELAY = "http://relay.example"


@pytest.fixture
def keydir(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path / "keys")
    return tmp_path / "keys"


def test_a_stored_token_is_reused_and_private(keydir):
    session_cache.store(RELAY, "alice", "tok-1", 10_000.0, "stable-1")
    assert session_cache.load(RELAY, "alice", now=1_000.0) == ("tok-1", 10_000.0, "stable-1")
    files = list((keydir / "sessions").iterdir())
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE((keydir / "sessions").stat().st_mode) == 0o700


def test_not_reused_shortly_before_expiry_or_for_another_relay_or_agent(keydir):
    session_cache.store(RELAY, "alice", "tok-1", 10_000.0)
    assert session_cache.load(RELAY, "alice", now=10_000.0 - session_cache.REUSE_MARGIN + 1) is None
    assert session_cache.load("http://other.example", "alice", now=1.0) is None
    assert session_cache.load(RELAY, "bob", now=1.0) is None


def test_forget_and_corrupt_files(keydir):
    session_cache.store(RELAY, "alice", "tok-1", 10_000.0)
    session_cache.forget(RELAY, "alice")
    assert session_cache.load(RELAY, "alice", now=1.0) is None
    session_cache.store(RELAY, "alice", "tok-1", 10_000.0)
    next((keydir / "sessions").iterdir()).write_text("not json")
    assert session_cache.load(RELAY, "alice", now=1.0) is None
    session_cache.store(RELAY, "alice", "tok-1", 10_000.0)
    p = next((keydir / "sessions").iterdir())
    p.write_text(json.dumps({"token": 5, "expires_at": "x"}))
    assert session_cache.load(RELAY, "alice", now=1.0) is None


def test_the_sdk_reuses_a_kept_token_without_signing_in_and_drops_it_on_invalidate(keydir):
    import time

    from emporia.agent_sdk import EmporiaAgent
    session_cache.store(RELAY, "alice", "kept-token", time.time() + 3000)

    async def go():
        async with EmporiaAgent(relay_url=RELAY, agent_id="alice", public_key_hex="", profile_id="alice") as a:
            # no relay is reachable: a sign-in would return None, so a token proves the file was used
            assert await a._bearer_token_value() == "kept-token"
            a._invalidate_bearer()
    asyncio.run(go())
    assert session_cache.load(RELAY, "alice") is None


def test_the_mcp_server_reuses_a_kept_token(keydir, monkeypatch):
    import time

    from emporia import mcp_server
    session_cache.store(RELAY, "alice", "kept-token", time.time() + 3000)
    monkeypatch.setattr(mcp_server.httpx, "post", lambda *a, **k: pytest.fail("signed in again"))
    mcp_server._dashboard_token_cache.clear()
    assert mcp_server._dashboard_bearer_headers(RELAY, "alice") == {"Authorization": "Bearer kept-token"}
    mcp_server._invalidate_dashboard_bearer(RELAY, "alice")
    assert session_cache.load(RELAY, "alice") is None
