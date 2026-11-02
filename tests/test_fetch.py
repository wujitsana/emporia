"""P270: another relay's answer is read within limits, and a hosted relay behind
its proxy rate-limits each client, not the proxy."""
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from emporia import fetch


class _Hostile(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if self.path == "/huge":
            self.end_headers()
            chunk = b" " * 65536
            try:
                for _ in range(200):                     # 12.5 MB, more than the cap
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == "/drip":
            self.end_headers()
            try:
                for _ in range(100):                     # a byte every 0.2 s: never a read timeout
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            body = json.dumps({"ok": True}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    do_POST = do_GET


@pytest.fixture
def hostile(monkeypatch):
    monkeypatch.setattr(fetch, "_PASSTHROUGH", False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Hostile)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_an_ordinary_answer_reads_as_usual(hostile):
    r = fetch.get(hostile + "/fine")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_a_body_past_the_cap_is_refused(hostile):
    with pytest.raises(fetch.TooLarge, match="more than"):
        fetch.get(hostile + "/huge", max_bytes=1024 * 1024)


def test_a_drip_is_cut_off_at_the_total_limit(hostile):
    start = time.monotonic()
    with pytest.raises(fetch.TooLarge, match="longer than"):
        fetch.get(hostile + "/drip", total=1.0)
    assert time.monotonic() - start < 5


def test_the_relays_own_calls_to_a_peer_are_capped_too(hostile):
    with pytest.raises(fetch.TooLarge):
        asyncio.run(fetch.request("POST", hostile + "/huge", content=b"{}", max_bytes=1024 * 1024))
    assert asyncio.run(fetch.request("GET", hostile + "/fine")).json() == {"ok": True}


def test_scan_relays_survives_a_relay_that_answers_too_much(hostile, monkeypatch):
    from emporia import mcp_server
    monkeypatch.setattr(mcp_server, "_linked_relays", lambda: [])
    monkeypatch.setattr(mcp_server, "RELAY_URL", hostile + "/fine")
    monkeypatch.setattr(fetch, "MAX_BYTES", 1024 * 1024)
    out = mcp_server.scan_relays(relays=[hostile + "/huge"])
    assert out["success"] and hostile + "/huge" in out["skipped"]


def test_the_hosted_relay_believes_its_proxy_about_who_is_calling():
    from emporia import relay_host
    import yaml
    compose = yaml.safe_load(relay_host.compose_yaml("../..", True))
    assert "FORWARDED_ALLOW_IPS=*" in compose["services"]["relay"]["environment"]
    assert compose["services"]["relay"]["ports"] == ["127.0.0.1:8088:8088"] or \
        all(p.startswith("127.0.0.1:") for p in compose["services"]["relay"]["ports"])   # only this machine


def test_behind_a_proxy_each_client_has_its_own_rate_limit():
    """What uvicorn does with FORWARDED_ALLOW_IPS: the client is the forwarded
    address, so ten sign-ins from one address do not lock out another."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from emporia import relay_server as rs
    proxied = TestClient(ProxyHeadersMiddleware(rs.app, trusted_hosts="*"), client=("172.18.0.5", 40000))
    try:
        codes = [proxied.post("/dashboard/challenge", headers={"X-Forwarded-For": "198.51.100.7"}).status_code
                 for _ in range(rs._RATE_LIMITS["auth"][0] + 1)]
        assert codes[-1] == 429 and set(codes[:-1]) == {200}
        assert proxied.post("/dashboard/challenge", headers={"X-Forwarded-For": "198.51.100.8"}).status_code == 200
    finally:
        for ip in ("198.51.100.7", "198.51.100.8", "172.18.0.5"):
            for bucket in rs._RATE_LIMITS:
                rs._RATE_WINDOWS.pop((ip, bucket), None)


def test_one_address_cannot_hold_more_than_its_share_of_websockets(monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    from emporia import relay_server as rs
    monkeypatch.setattr(rs, "_WS_PER_ADDRESS", 2)
    far = TestClient(rs.app, client=("203.0.113.50", 40001))
    other = TestClient(rs.app, client=("203.0.113.51", 40002))
    with far.websocket_connect("/ws/events"), far.websocket_connect("/ws/events"):
        with pytest.raises(WebSocketDisconnect) as refused:
            with far.websocket_connect("/ws/events") as ws:
                ws.receive_json()
        assert refused.value.code == 1013
        with other.websocket_connect("/ws/events"):          # another address is not affected
            pass
    assert rs._WS_OPEN.get("203.0.113.50", 0) == 0           # closed sockets are given back
    with far.websocket_connect("/ws/events"):
        pass
