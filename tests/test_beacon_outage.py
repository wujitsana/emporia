"""A beacon outage must not freeze the relay: failures are remembered, the Lore never waits on drand for long,
and a chapter whose round is late simply waits."""
from __future__ import annotations

import asyncio
import threading
import time

from emporia import rand
from emporia import relay_server as rs  # noqa: F401  (loads the app before its routes)
from emporia.routes import beacon as beacon_route
from emporia.routes import lore as ch

REAL_SIGNATURE = beacon_route.signature     # captured before the suite's autouse fake replaces it


def test_a_failed_round_is_not_asked_for_again_for_a_while(monkeypatch):
    calls = []

    def dead(url):
        calls.append(url)
        raise OSError("unreachable")
    monkeypatch.setattr(beacon_route, "_FETCH", dead)
    beacon_route._MISS.clear()
    rnd = rand.round_at(time.time()) - 50
    assert REAL_SIGNATURE(rnd) is None
    first = len(calls)
    assert first >= 1
    for _ in range(5):
        assert REAL_SIGNATURE(rnd) is None
    assert len(calls) == first, "the next callers did not hammer the hosts"
    beacon_route._MISS[rnd] -= beacon_route.MISS_TTL_S + 1      # the memory expires
    assert REAL_SIGNATURE(rnd) is None and len(calls) > first
    beacon_route._MISS.clear()


def test_a_hanging_beacon_does_not_stall_the_event_loop_or_the_lore(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(beacon_route, "signature", lambda r: gate.wait(30) and None)
    monkeypatch.setattr(ch, "BEACON_WAIT_S", 0.1)
    monkeypatch.setattr(ch, "_BEACONS", {})
    monkeypatch.setattr(ch, "_INFLIGHT", set())
    at = time.time()
    start = at - 3 * 86_400

    async def go():
        stalls = []

        async def ticker():
            last = time.monotonic()
            for _ in range(15):
                await asyncio.sleep(0.02)
                now = time.monotonic()
                stalls.append(now - last)
                last = now
        t = asyncio.create_task(ticker())
        t0 = time.monotonic()
        for _ in range(3):                       # repeated calls: the fetch already in flight is not started again
            assert ch._beacons(start, 86_400, at) == {}
        took = time.monotonic() - t0
        await t
        return took, max(stalls)
    try:
        took, worst = asyncio.run(go())
    finally:
        gate.set()
    assert took < 1.0, took            # one bounded wait, then the in-flight fetch is left alone
    assert worst < 0.6, worst


def test_a_beacon_that_raises_is_a_wait_and_a_late_round_is_picked_up(monkeypatch):
    state = {"up": False}

    def flaky(r):
        if not state["up"]:
            raise OSError("down")
        return "ab" * 48
    monkeypatch.setattr(beacon_route, "signature", flaky)
    monkeypatch.setattr(ch, "_BEACONS", {})
    monkeypatch.setattr(ch, "_INFLIGHT", set())
    at = time.time()
    start = at - 86_400
    assert ch._beacons(start, 86_400, at) == {}            # no crash
    state["up"] = True
    got = ch._beacons(start, 86_400, at)
    assert got and all(v == "ab" * 48 for v in got.values())
