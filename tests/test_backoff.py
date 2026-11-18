"""Retries wait with exponential backoff and full jitter, and an unregistered agent waits much longer."""

import asyncio
from types import SimpleNamespace

import httpx

from emporia import backoff
from emporia.plugins.platforms.emporia import tunnel


def test_full_jitter_is_between_the_floor_and_the_doubling_ceiling_and_capped():
    for attempt, ceiling in [(0, 1), (1, 2), (2, 4), (5, 32), (6, 60), (30, 60), (99, 60)]:
        draws = [backoff.full_jitter(attempt, 1.0, 60.0, rng=lambda r=r: r) for r in (0.0, 0.5, 0.999999)]
        assert all(backoff.MIN_WAIT * (ceiling >= backoff.MIN_WAIT) <= d <= ceiling for d in draws), (attempt, draws)
    # a different draw gives a different wait: agents restarted together do not retry together
    assert backoff.full_jitter(4, rng=lambda: 0.1) != backoff.full_jitter(4, rng=lambda: 0.9)
    # a tiny ceiling (a test's 0.01) is respected, never raised to the floor
    assert backoff.full_jitter(0, 0.01, 60.0, rng=lambda: 0.0) == 0.01


def test_an_unregistered_agent_waits_a_minute_at_least_and_grows_to_about_fifteen():
    lo = [backoff.refusal_wait(404, a, rng=lambda: 0.0) for a in range(8)]
    hi = [backoff.refusal_wait(404, a, rng=lambda: 0.999999) for a in range(8)]
    assert min(lo) >= backoff.UNREGISTERED_FLOOR
    assert hi[-1] <= backoff.UNREGISTERED_CAP + 1e-6 and hi[-1] > 800
    assert all(b >= a for a, b in zip(hi, hi[1:], strict=False))


def test_a_429_waits_at_least_what_the_relay_asked():
    assert backoff.refusal_wait(429, 0, 30.0, rng=lambda: 0.0) >= 30.0
    assert backoff.refusal_wait(429, 3, None, rng=lambda: 0.5) > 0


def test_the_tunnel_never_spins_on_a_404_or_429(monkeypatch):
    """A bridge for an agent the relay does not know: every wait is at least a minute; a 429 waits as asked."""
    from emporia import agent_sdk

    def refuse(status):
        req = httpx.Request("POST", "http://relay.example/dashboard/session")
        return httpx.HTTPStatusError("x", request=req, response=httpx.Response(
            status, headers={"retry-after": "7"} if status == 429 else {}, request=req))

    for status, floor in ((404, 60.0), (429, 7.0)):
        calls, sleeps = [], []

        async def mint(self, status=status, calls=calls):
            calls.append(1)
            raise refuse(status)

        async def fake_sleep(t, sleeps=sleeps):
            sleeps.append(t)
            if len(sleeps) >= 4:
                raise asyncio.CancelledError

        monkeypatch.setattr(agent_sdk.EmporiaAgent, "_mint_bearer", mint)
        monkeypatch.setattr(tunnel.asyncio, "sleep", fake_sleep)

        async def go():
            try:
                await tunnel.maintain_relay_tunnel("http://relay.example", "ghost", lambda f: None, profile_id="ghost")
            except asyncio.CancelledError:    # the fake sleep's way of ending the loop
                pass
        asyncio.run(go())
        assert len(calls) == 4 and len(sleeps) == 4, (status, calls, sleeps)
        assert all(s >= floor for s in sleeps), (status, sleeps)


def test_refusal_wait_reads_a_response_without_headers():
    assert tunnel._refusal_wait(SimpleNamespace(status_code=500), 0) >= 0


def test_the_bridge_does_not_tell_the_person_to_register_in_its_first_minute(monkeypatch, caplog):
    """The MCP server's own start-up registers the agent a moment after the bridge starts: the instruction to run a
    command is logged only once the bridge has waited a minute; before that it is an info line without one."""
    import logging

    from emporia import agent_sdk
    req = httpx.Request("POST", "http://relay.example/dashboard/session")

    async def mint(self):
        raise httpx.HTTPStatusError("x", request=req, response=httpx.Response(404, request=req))

    clock = [1000.0]
    sleeps = []

    async def fake_sleep(t):
        sleeps.append(t)
        clock[0] += 30.0                 # 30 s per wait: attempts at 0, 30, 60, 90 s
        if len(sleeps) >= 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(agent_sdk.EmporiaAgent, "_mint_bearer", mint)
    monkeypatch.setattr(tunnel.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(tunnel, "_now", lambda: clock[0])

    async def go():
        try:
            await tunnel.maintain_relay_tunnel("http://relay.example", "ghost", lambda f: None, profile_id="ghost")
        except asyncio.CancelledError:
            pass
    with caplog.at_level(logging.INFO, logger=tunnel.logger.name):
        asyncio.run(go())
    early = [r for r in caplog.records if r.levelno == logging.INFO and "not registered" in r.getMessage()]
    late = [r for r in caplog.records if r.levelno == logging.WARNING and "not registered" in r.getMessage()]
    assert len(early) == 1 and "hermes emporia register" not in early[0].getMessage()
    assert len(late) == 1 and "hermes emporia register ghost" in late[0].getMessage()
    assert caplog.records.index(early[0]) < caplog.records.index(late[0])
