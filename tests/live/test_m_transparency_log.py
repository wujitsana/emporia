"""P281 (m): the transparency log checked for real — an agent keeps its relay's signed state and checks the
next one only added to it, and a linked relay keeps the same relay's state at every sync, so the agent can
compare what it was shown with what the other relay was shown."""
import time

import httpx


def _state_after(relay, bigger_than: int = 0, timeout: float = 40) -> dict:
    """The relay's signed log state, once it covers more than `bigger_than` entries."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = httpx.get(f"{relay.url}/log/state", timeout=5)
        if r.status_code == 200 and r.json()["size"] > bigger_than:
            return r.json()
        time.sleep(1)
    raise AssertionError(f"{relay.name}: no signed log state past {bigger_than} entries in {timeout}s")


def test_an_agent_and_a_linked_relay_check_the_same_log(fleet):
    a, b = fleet.a, fleet.b
    assert httpx.get(f"{a.url}/log/state", timeout=5).status_code == 404        # off unless turned on
    a.extra.update({"EMPORIA_TRANSPARENCY_LOG": "on", "EMPORIA_LOG_STATE_INTERVAL_SECONDS": "1"})
    a.restart()
    alpha = fleet.registered("alpha", a)
    assert alpha.tool("post", text="Log lessons: one hour", kind="offer")["success"]
    first = _state_after(a)

    out = alpha.tool("check_relay_log")
    assert out["ok"] and out["status"] == "first", out
    assert {c["relay"]: c["status"] for c in out["compared_with"]}.get(b.url) == "unchecked", out   # b saw none yet

    b.sync()                                             # b pulls from a, and checks a's log as it does
    seen = httpx.get(f"{b.url}/log/seen", timeout=5).json()["relays"]
    assert seen[a.url]["status"] == "first" and seen[a.url]["state"]["size"] >= first["size"], seen
    assert b.peers()[a.url]["log"]["status"] == "first"

    assert alpha.tool("post", text="More log lessons: two hours", kind="offer")["success"]
    _state_after(a, bigger_than=max(first["size"], seen[a.url]["state"]["size"]))
    out = alpha.tool("check_relay_log")
    assert out["ok"] and out["status"] == "ok" and "kept what it had" in out["detail"], out
    assert {c["relay"]: c["status"] for c in out["compared_with"]}[b.url] == "ok", out             # same log
    b.sync()
    assert httpx.get(f"{b.url}/log/seen", timeout=5).json()["relays"][a.url]["status"] == "ok"
