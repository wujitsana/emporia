"""The HTTP/SSE MCP transport has no sign-in: it runs on loopback only, on an explicit opt-in, and a Host header
that is not this machine gets 403."""
import pytest
from starlette.testclient import TestClient

from emporia import mcp_server as ms


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "::"])
def test_a_non_loopback_bind_is_refused(host):
    assert "EMPORIA_MCP_HTTP_LOCAL_ONLY=1" in ms._http_refusal(host, "1")


def test_loopback_needs_the_opt_in():
    for h in ("127.0.0.1", "localhost", "::1"):
        assert "EMPORIA_MCP_HTTP_LOCAL_ONLY=1" in ms._http_refusal(h, "")
        assert ms._http_refusal(h, "1") == ""


def test_main_exits_with_one_line_and_never_runs(monkeypatch):
    ran = []
    monkeypatch.setattr(ms.mcp, "run", lambda *a, **k: ran.append(k))
    monkeypatch.setenv("EMPORIA_MCP_TRANSPORT", "http")
    monkeypatch.delenv("EMPORIA_MCP_HTTP_LOCAL_ONLY", raising=False)
    monkeypatch.setattr(ms, "_http_host", lambda: "127.0.0.1")
    with pytest.raises(SystemExit) as e:
        ms.main()
    assert "\n" not in str(e.value.code) and "EMPORIA_MCP_HTTP_LOCAL_ONLY" in str(e.value.code)
    monkeypatch.setattr(ms, "_http_host", lambda: "0.0.0.0")
    monkeypatch.setenv("EMPORIA_MCP_HTTP_LOCAL_ONLY", "1")
    with pytest.raises(SystemExit):
        ms.main()
    assert ran == []


def test_main_runs_on_loopback_with_the_guard(monkeypatch):
    ran = []
    monkeypatch.setattr(ms.mcp, "run", lambda *a, **k: ran.append(k))
    monkeypatch.setenv("EMPORIA_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("EMPORIA_MCP_HTTP_LOCAL_ONLY", "1")
    monkeypatch.setattr(ms, "_http_host", lambda: "127.0.0.1")
    ms.main()
    assert ran[0]["host"] == "127.0.0.1" and ran[0]["middleware"]


@pytest.mark.parametrize("h,ok", [("127.0.0.1", True), ("127.0.0.1:8089", True), ("localhost:1", True),
                                  ("[::1]:8089", True), ("[::1]", True), ("evil.com", False),
                                  ("evil.com:8089", False), ("127.0.0.1.evil.com", False), ("", False),
                                  ("localhost:x", False), ("[::1]x", False)])
def test_host_header_check(h, ok):
    assert ms._host_allowed(h) is ok


def test_a_foreign_host_header_gets_403_and_a_local_one_does_not():
    app = ms.mcp.http_app(middleware=ms._http_middleware(), host_origin_protection=False)
    c = TestClient(app, base_url="http://127.0.0.1:8089")
    assert c.post("/mcp", headers={"host": "evil.com"}, json={}).status_code == 403
    assert c.post("/mcp", headers={"host": "evil.com:8089"}, json={}).status_code == 403
    assert c.post("/mcp", headers={"host": "127.0.0.1:8089"}, json={}).status_code != 403
    assert c.post("/mcp", headers={"host": "127.0.0.1:8089", "origin": "http://evil.com"}, json={}).status_code == 403
    assert c.post("/mcp", headers={"host": "127.0.0.1:8089", "origin": "http://localhost:3000"}, json={}).status_code != 403
