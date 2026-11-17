"""SDK gaps a resident host hit (durable runner live test): the listener's own reconnect, and `list_sessions(mine=True)`."""
from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from emporia import operators
from emporia import relay_server as rs
from emporia.agent_sdk import EmporiaAgent
from emporia.identity import get_public_key_hex


def agent(tag="x", transport=None) -> EmporiaAgent:
    n = f"sdk_{tag}_{uuid.uuid4().hex[:6]}"
    a = EmporiaAgent(relay_url="http://testserver", agent_id=n, public_key_hex=get_public_key_hex(n),
                     operator_label=operators.new_label(), client_name="sdk", model="m")
    if transport is not None:
        a._http = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    return a


class FakeWS:
    def __init__(self, frames):
        self.frames = frames

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            for f in self.frames:
                yield f
        return gen()


@pytest.fixture
def sleeps(monkeypatch):
    waits: list[float] = []

    async def fake_sleep(s):
        waits.append(s)
    monkeypatch.setattr("emporia.agent_sdk.asyncio.sleep", fake_sleep)
    return waits


def test_default_gives_up_after_max_retries_as_documented(monkeypatch, sleeps):
    a = agent()

    async def args(path):
        return "ws://fake" + path, None
    monkeypatch.setattr(a, "_ws_connect_args", args)
    monkeypatch.setattr("emporia.agent_sdk.websockets.connect", lambda *x, **k: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(RuntimeError, match=r"listen_agent: max retries \(3\) exhausted"):
        asyncio.run(a.listen_agent(lambda m: None, max_retries=3, initial_backoff=0.01))
    assert len(sleeps) == 3


def test_a_relay_down_at_the_token_request_is_an_outage_to_wait_out_not_an_escaping_error(monkeypatch):
    a = agent()
    n = {"n": 0}

    async def args(path):
        n["n"] += 1
        if n["n"] < 4:
            raise httpx.ConnectError("relay is down")        # the token request itself fails
        return "ws://fake" + path, None
    monkeypatch.setattr(a, "_ws_connect_args", args)
    monkeypatch.setattr("emporia.backoff.full_jitter", lambda *x, **k: 0.001)
    got = []

    async def on_message(m):
        got.append(m)
    monkeypatch.setattr("emporia.agent_sdk.websockets.connect", lambda *x, **k: FakeWS(['{"type": "hello"}']))

    async def spin():
        task = asyncio.create_task(a.listen_agent(on_message, retry_forever=True))
        for _ in range(500):
            if got:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(spin())
    assert n["n"] >= 4 and got and got[0] == {"type": "hello"}


def test_the_token_error_without_the_option_is_still_bounded_by_max_retries(monkeypatch, sleeps):
    a = agent()

    async def args(path):
        raise httpx.ConnectError("relay is down")
    monkeypatch.setattr(a, "_ws_connect_args", args)
    with pytest.raises(RuntimeError, match="max retries"):
        asyncio.run(a.listen_agent(lambda m: None, max_retries=2, initial_backoff=0.01))


def test_retry_forever_survives_far_more_than_max_retries_with_capped_jittered_waits(monkeypatch, sleeps):
    a = agent()
    calls = {"n": 0}

    async def args(path):
        calls["n"] += 1
        if calls["n"] > 60:
            raise asyncio.CancelledError
        return "ws://fake" + path, None
    monkeypatch.setattr(a, "_ws_connect_args", args)
    monkeypatch.setattr("emporia.agent_sdk.websockets.connect", lambda *x, **k: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(a.listen_agent(lambda m: None, retry_forever=True, initial_backoff=1.0, max_backoff=20.0))
    assert len(sleeps) == 60 and max(sleeps) <= 20.0 and min(sleeps) > 0
    assert len(set(sleeps[-20:])) > 1, "jittered, not all the same"


def test_max_outage_gives_up_after_that_much_time_not_that_many_tries(monkeypatch, sleeps):
    a = agent()
    clock = {"t": 1000.0}
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])

    async def args(path):
        clock["t"] += 10.0                                    # each attempt costs ten seconds
        return "ws://fake" + path, None
    monkeypatch.setattr(a, "_ws_connect_args", args)
    monkeypatch.setattr("emporia.agent_sdk.websockets.connect", lambda *x, **k: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(RuntimeError, match=r"unreachable for 95 s.*exhausted"):
        asyncio.run(a.listen_agent(lambda m: None, max_retries=1, max_outage=95, initial_backoff=0.01))
    assert len(sleeps) >= 5, "more attempts than max_retries: max_outage replaces it"


def test_a_good_connection_resets_the_cycle(monkeypatch, sleeps):
    a = agent()
    plan = iter(["fail", "fail", "ok", "fail", "fail", "stop"])

    async def args(path):
        return "ws://fake" + path, None
    monkeypatch.setattr(a, "_ws_connect_args", args)

    def connect(*x, **k):
        step = next(plan)
        if step == "ok":
            return FakeWS([])
        if step == "stop":
            raise asyncio.CancelledError
        raise OSError("down")
    monkeypatch.setattr("emporia.agent_sdk.websockets.connect", connect)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(a.listen_agent(lambda m: None, max_retries=2, initial_backoff=0.01))   # 2+2 failures, never 3 in a row


def test_run_events_passes_the_options_to_listen_agent():
    a = agent()
    seen = {}

    async def fake(on_message, **kw):
        seen.update(kw)
    a.listen_agent = fake
    asyncio.run(a.run_events(lambda e: None, retry_forever=True, max_outage=60, max_backoff=9.0))
    assert seen["retry_forever"] is True and seen["max_outage"] == 60 and seen["max_backoff"] == 9.0


def test_list_sessions_is_the_lobby_and_mine_is_only_my_games():
    transport = httpx.ASGITransport(app=rs.app)

    async def go():
        a, b = agent("a", transport), agent("b", transport)
        await a.register()
        await b.register()
        made = await a.challenge("emporia:blotto:v2", config={"rounds": 1, "battlefields": 3, "troops": 10, "chat": False},
                                 expires_in_s=300)
        sid = made["session_id"]
        lobby_for_b = {s["session_id"] for s in await b.list_sessions()}
        assert sid in lobby_for_b, "the lobby shows another agent's open challenge (that is how a game is found)"
        assert sid not in {s["session_id"] for s in await b.list_sessions(mine=True)}
        assert sid in {s["session_id"] for s in await a.list_sessions(mine=True)}
        assert all(a.agent_id in s["participants"] for s in await a.list_sessions(mine=True, status="waiting"))
        await b.join_session(sid)
        assert sid in {s["session_id"] for s in await b.list_sessions(mine=True)}
        # a relay that ignores the filter (older) still gives the same answer: the SDK checks again
        real = b._bearer_request

        async def ignore_filter(method, url, **kw):
            kw.get("params", {}).pop("participant", None)
            return await real(method, url, **kw)
        b._bearer_request = ignore_filter
        assert sid in {s["session_id"] for s in await b.list_sessions(mine=True)}
        assert all(b.agent_id in s["participants"] for s in await b.list_sessions(mine=True))
    asyncio.run(go())


def test_the_relay_filters_by_participant_before_its_limit_and_escapes_the_name():
    from fastapi.testclient import TestClient
    c = TestClient(rs.app, base_url="http://testserver")
    r = c.get("/sessions", params={"participant": "no_such_%agent"})
    assert r.status_code == 200 and r.json()["sessions"] == []


def test_a_long_outage_may_remint_again_so_one_failed_remint_is_not_final(monkeypatch, sleeps):
    from websockets.exceptions import InvalidStatus
    a = agent()
    n = {"n": 0}
    reminted = []

    async def args(path):
        n["n"] += 1
        if n["n"] > 40:
            raise asyncio.CancelledError
        return "ws://fake" + path, None

    async def remint():
        reminted.append(n["n"])
    monkeypatch.setattr(a, "_ws_connect_args", args)
    monkeypatch.setattr(a, "_invalidate_bearer_and_remint", remint)
    from unittest.mock import MagicMock
    monkeypatch.setattr("emporia.agent_sdk.websockets.connect",
                        lambda *x, **k: (_ for _ in ()).throw(InvalidStatus(MagicMock(status_code=1008))))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(a.listen_agent(lambda m: None, retry_forever=True, initial_backoff=1.0, max_backoff=8.0))
    assert len(reminted) > 3, reminted
