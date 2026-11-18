"""A relay behind a CDN answers gzip: `fetch` reads it within limits and the rebuilt answer reads once, not twice."""
import gzip
import json

import httpx

from emporia import fetch


def _gzip_transport(payload: dict):
    body = gzip.compress(json.dumps(payload).encode())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json", "content-encoding": "gzip",
                                            "content-length": str(len(body))}, content=body)
    return httpx.MockTransport(handler)


def test_a_gzipped_answer_is_read_once(monkeypatch):
    payload = {"size": 486, "root": "ab" * 32}
    real_client = httpx.Client
    monkeypatch.setattr(fetch, "_PASSTHROUGH", False)
    monkeypatch.setattr(fetch.httpx, "Client", lambda **kw: real_client(transport=_gzip_transport(payload), **kw))
    r = fetch.get("https://relay.example/log/state")
    assert r.status_code == 200 and r.json() == payload
    assert "content-encoding" not in {k.lower() for k in r.headers}


def test_a_gzipped_answer_is_read_once_async(monkeypatch):
    import asyncio
    payload = {"ok": True}
    real_client = httpx.AsyncClient

    def handler(request):
        body = gzip.compress(json.dumps(payload).encode())
        return httpx.Response(200, headers={"content-encoding": "gzip"}, content=body)
    monkeypatch.setattr(fetch.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    r = asyncio.run(fetch.request("GET", "https://relay.example/x"))
    assert r.json() == payload
