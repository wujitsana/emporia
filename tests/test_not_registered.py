"""An agent the relay does not know is told so (and how to fix it), not given a bare 403."""
import httpx

from emporia import mcp_server


def _relay(monkeypatch, *, known):
    calls = []

    def post(url, **kw):
        calls.append(url)
        req = httpx.Request("POST", url)
        if url.endswith("/dashboard/challenge"):
            return httpx.Response(200, json={"challenge_id": "c", "nonce": "00" * 32}, request=req)
        return httpx.Response(200 if known else 404, json={"token": "t", "expires_in": 3600} if known
                              else {"detail": "Agent 'ghost' not registered on this relay"}, request=req)

    def get(url, **kw):
        return httpx.Response(403, json={"detail": "only the agent itself reads what is next for it"},
                              request=httpx.Request("GET", url))
    monkeypatch.setattr(mcp_server.httpx, "post", post)
    monkeypatch.setattr(mcp_server.httpx, "get", get)
    monkeypatch.setattr(mcp_server.signing, "dashboard_session_body", lambda *a, **k: {})
    monkeypatch.setattr("emporia.session_cache.load", lambda *a, **k: None)
    monkeypatch.setattr("emporia.session_cache.store", lambda *a, **k: None)
    mcp_server._dashboard_token_cache.clear()
    mcp_server._unregistered_at.clear()
    return calls


def test_whats_next_says_not_registered_and_does_not_sign_in_again_at_once(monkeypatch):
    monkeypatch.setattr(mcp_server, "AGENT_ID", "ghost")
    monkeypatch.setattr(mcp_server, "RELAY_URL", "http://relay.example")
    calls = _relay(monkeypatch, known=False)
    out = mcp_server.whats_next()
    assert out["success"] is False and out["registered"] is False
    assert "not registered on this relay yet" in out["error"] and "emporia register ghost" in out["error"]
    assert "(or ask your person)" in out["error"]
    first = len(calls)
    mcp_server.whats_next()
    assert len(calls) == first, "a sign-in that just failed with 404 is not retried within the minute"
    mcp_server._unregistered_at.clear()


def test_a_registered_agents_403_keeps_its_own_words(monkeypatch):
    monkeypatch.setattr(mcp_server, "AGENT_ID", "ghost")
    monkeypatch.setattr(mcp_server, "RELAY_URL", "http://relay.example")
    _relay(monkeypatch, known=True)
    out = mcp_server.whats_next()
    assert out["error"] == "Relay 403" and "only the agent itself" in str(out["detail"])


def test_the_message_names_the_plugin_command_where_the_plugin_runs_it(monkeypatch):
    monkeypatch.setattr(mcp_server, "localize", lambda t: t.replace("`emporia", "`hermes emporia"))
    assert "hermes emporia register ghost" in mcp_server.not_registered_message("ghost")
