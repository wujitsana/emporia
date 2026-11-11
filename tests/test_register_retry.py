"""Agents behind one address share the relay's per-IP sign-in budget (live 11-10: a gateway restarting four agents
went over it). Startup registration retries after a 429 instead of giving up, and the bridge's tunnel waits as asked
instead of re-signing-in every few seconds."""
from types import SimpleNamespace

from emporia import mcp_server
from emporia.plugins.platforms.emporia import tunnel


def _resp(status, retry_after=None):
    return SimpleNamespace(status_code=status, headers={"retry-after": retry_after} if retry_after else {})


def test_the_tunnel_waits_as_the_relay_asks():
    assert tunnel._refusal_wait(_resp(429, "12"), 1.0, 60.0) == 12.0
    assert tunnel._refusal_wait(_resp(429), 1.0, 60.0) == 30.0
    assert tunnel._refusal_wait(_resp(404), 1.0, 60.0) == 60.0        # not registered yet: never a tight loop
    assert tunnel._refusal_wait(_resp(404), 120.0, 60.0) == 120.0


def test_a_busy_relay_is_tried_again_and_a_real_failure_is_not(monkeypatch):
    answers = iter([{"success": False, "error": "challenge failed: Client error '429 Too Many Requests'"},
                    {"success": False, "error": "challenge failed: Client error '429 Too Many Requests'"},
                    {"success": True}])
    calls = []
    monkeypatch.setattr(mcp_server, "AGENT_ID", "retry_a")
    monkeypatch.setattr(mcp_server, "register_agent", lambda **kw: calls.append(kw) or next(answers))
    monkeypatch.setattr(mcp_server, "_heal_x402_pay_to", lambda: None)
    assert mcp_server._register_once() == "busy"
    slept = []
    assert mcp_server._register_later(waits=(20, 40, 60), sleep=slept.append) is True
    assert len(calls) == 3 and len(slept) == 2 and slept[0] >= 20
    monkeypatch.setattr(mcp_server, "register_agent", lambda **kw: {"success": False, "error": "403 refused"})
    assert mcp_server._register_once() == "failed"


def test_startup_does_not_block_on_a_busy_relay(monkeypatch):
    started = []
    monkeypatch.setattr(mcp_server, "AGENT_ID", "retry_b")
    monkeypatch.setattr(mcp_server, "_register_once", lambda: "busy")
    import threading
    monkeypatch.setattr(threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: started.append(kw["name"])))
    mcp_server._auto_register()
    assert started == ["emporia-register-retry"]
