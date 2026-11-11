"""Request-body ceiling for chunked bodies, deeply nested JSON, and the plaintext scan cap."""

from __future__ import annotations

from fastapi import Request
from fastapi.testclient import TestClient

from emporia.relay_server import MAX_REQUEST_BYTES, app


async def probe(request: Request):
    return await request.json()


def _client() -> TestClient:
    return TestClient(app, client=("127.0.0.1", 51691), raise_server_exceptions=False)


def _chunks(total: int, size: int = 64 * 1024):
    sent = 0
    while sent < total:
        n = min(size, total - sent)
        sent += n
        yield b"a" * n


def test_chunked_body_over_the_limit_is_413_on_an_unauthenticated_route():
    r = _client().post("/agents/register", content=_chunks(MAX_REQUEST_BYTES * 4))
    assert r.status_code == 413, r.status_code
    assert "too large" in r.json()["detail"]


def test_chunked_body_over_the_limit_is_413_on_any_post():
    r = _client().post("/dashboard/session", content=_chunks(MAX_REQUEST_BYTES + 1),
                       headers={"content-type": "application/json"})
    assert r.status_code == 413, r.status_code


def test_chunked_body_under_the_limit_still_reaches_the_handler():
    r = _client().post("/agents/register", content=_chunks(2048))
    assert r.status_code != 413, r.status_code


def test_deeply_nested_json_is_400_not_500():
    """A handler that parses the body itself (`await request.json()`) used to answer 500 here."""
    route_count = len(app.router.routes)
    app.router.add_api_route("/__nested_probe", probe, methods=["POST"])
    try:
        body = "[" * 200_000 + "]" * 200_000
        r = _client().post("/__nested_probe", content=body, headers={"content-type": "application/json"})
    finally:
        del app.router.routes[route_count:]
    assert r.status_code == 400, (r.status_code, r.text[:200])
    assert "nested" in r.json()["detail"]


def test_plaintext_over_32_kib_is_413_but_ciphertext_keeps_the_larger_limit():
    import asyncio

    import pytest
    from fastapi import HTTPException

    from emporia import relay_server as rs
    from emporia.engine.guardrails import MAX_PLAINTEXT_BYTES

    assert MAX_PLAINTEXT_BYTES == 32 * 1024
    asyncio.run(rs._assert_payload_safe_counted({"text": "hello " * 1000}))
    with pytest.raises(HTTPException) as e:
        asyncio.run(rs._assert_payload_safe_counted({"text": "x " * (MAX_PLAINTEXT_BYTES // 2 + 10)}))
    assert e.value.status_code == 413
    assert "unencrypted" in e.value.detail
    # a long identifier-shaped value under a structural key is not scanned, so not capped here
    with pytest.raises(HTTPException) as e2:
        asyncio.run(rs._assert_payload_safe_counted({"tags": ["y" * (MAX_PLAINTEXT_BYTES + 1)]}))
    assert e2.value.status_code == 413
