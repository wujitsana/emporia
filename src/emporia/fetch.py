"""Reading another relay's answer, with limits (P270).

A relay this side does not run can answer with anything: a body of gigabytes, or
one byte every few seconds forever (httpx's `timeout` is per read, so a drip
never trips it). Everything an agent or a relay reads from a relay it does not
control goes through here: the body is read as it arrives and refused past
`max_bytes`, and the whole exchange past `total` seconds. The relay's own
federation pulls have had the byte cap since P21-1 (`_fetch_peer_json`); this
gives the same to the agent's side (`scan_relays`, visits, cards, moves) and to
relay-to-relay DMs.

The answer is an ordinary, fully read `httpx.Response`, so callers keep using
`.status_code` and `.json()`.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

MAX_BYTES = 4 * 1024 * 1024        # a feed is capped at 200 signed items; this is far above it
TOTAL_SECONDS = 30.0

# Tests that stand in for other relays patch `httpx.get`; conftest sets this so
# the sync path goes through it (the limits themselves are tested with it off).
_PASSTHROUGH = False


class TooLarge(httpx.HTTPError):
    """The relay's answer went past the byte or time limit."""


def _finish(resp: httpx.Response, chunks, max_bytes: int, deadline: float) -> httpx.Response:
    body = bytearray()
    for chunk in chunks:
        body.extend(chunk)
        if len(body) > max_bytes:
            raise TooLarge(f"{resp.request.url.host} answered with more than {max_bytes} bytes")
        if time.monotonic() > deadline:
            raise TooLarge(f"{resp.request.url.host} took longer than the limit to answer")
    return httpx.Response(resp.status_code, headers=resp.headers, content=bytes(body), request=resp.request)


def get(url: str, *, params: dict[str, Any] | None = None, timeout: float = 10.0,
        max_bytes: int = MAX_BYTES, total: float = TOTAL_SECONDS) -> httpx.Response:
    """`httpx.get`, read within limits."""
    if _PASSTHROUGH:
        return httpx.get(url, timeout=timeout, **({"params": params} if params is not None else {}))
    deadline = time.monotonic() + total
    with httpx.Client(timeout=timeout) as client, client.stream("GET", url, params=params) as resp:
        return _finish(resp, resp.iter_bytes(), max_bytes, deadline)


async def request(method: str, url: str, *, content: bytes | None = None, headers: dict[str, str] | None = None,
                  timeout: float = 15.0, max_bytes: int = MAX_BYTES, total: float = TOTAL_SECONDS) -> httpx.Response:
    """An async request to another relay, read within limits."""
    deadline = time.monotonic() + total
    async with httpx.AsyncClient(timeout=timeout) as client, \
            client.stream(method, url, content=content, headers=headers) as resp:
        body = bytearray()
        async for chunk in resp.aiter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise TooLarge(f"{resp.request.url.host} answered with more than {max_bytes} bytes")
            if time.monotonic() > deadline:
                raise TooLarge(f"{resp.request.url.host} took longer than the limit to answer")
        return httpx.Response(resp.status_code, headers=resp.headers, content=bytes(body), request=resp.request)
