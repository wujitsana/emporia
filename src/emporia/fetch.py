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

import ipaddress
import socket
import time
from typing import Any
from urllib.parse import urlsplit

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


def is_public_address(raw: str) -> bool:
    """Whether an IP address is one a call to an address someone else chose may reach: global unicast, and not one
    that merely carries a private IPv4 address in an IPv6 one. The embedded IPv4 is extracted and checked for
    IPv4-mapped (::ffff:0:0/96), IPv4-translated (::ffff:0:0:0/96), IPv4-compatible (::/96), NAT64 (64:ff9b::/96)
    and 6to4 (2002::/16); NAT64 local-use (64:ff9b:1::/48) and site-local (fec0::/10) are refused outright."""
    try:
        ip = ipaddress.ip_address(str(raw).split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        n = int(ip)
        if ip in ipaddress.ip_network("64:ff9b:1::/48") or ip in ipaddress.ip_network("fec0::/10"):
            return False
        embedded = None
        if ip.ipv4_mapped is not None:
            embedded = ip.ipv4_mapped
        elif n >> 32 == 0 or n >> 32 == 0xFFFF0000:
            embedded = ipaddress.IPv4Address(n & 0xFFFFFFFF)          # ::/96 and ::ffff:0:0:0/96
        elif ip in ipaddress.ip_network("64:ff9b::/96"):
            embedded = ipaddress.IPv4Address(n & 0xFFFFFFFF)
        elif ip in ipaddress.ip_network("2002::/16"):
            embedded = ipaddress.IPv4Address((n >> 80) & 0xFFFFFFFF)
        if embedded is not None:
            return is_public_address(str(embedded))
    return ip.is_global and not ip.is_multicast


def resolve_public(url: str) -> tuple[str, list[str]]:
    """Resolve the host of `url` once: (why it may not be called, "") or ("", the addresses to connect to, all of
    them public). Callers connect to one of these exact addresses (`get(..., connect_ip=)`), never to the name again,
    so a name that answers differently the second time (DNS rebinding) reaches nothing."""
    parts = urlsplit(url)
    try:
        port = parts.port or 443            # a port above 65535 raises here
    except ValueError:
        return "not an https address", []
    if parts.scheme != "https" or not parts.hostname:
        return "not an https address", []
    try:
        found = list(dict.fromkeys(ai[4][0].split("%")[0] for ai in
                                   socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP)))
    except OSError:
        return "no such host", []
    if not found or not all(is_public_address(raw) for raw in found):
        return "not a public address", []
    return "", found


def _pinned(url: str, ip: str, headers: dict[str, str] | None) -> tuple[str, dict[str, str], dict[str, Any]]:
    """`url` with its host replaced by `ip`: the Host header and the TLS server name stay the name, so the
    certificate is still checked against the name."""
    parts = urlsplit(url)
    name = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    literal = f"[{ip}]" if ":" in ip else ip
    host_header = name + port
    return (parts._replace(netloc=f"{literal}{port}").geturl(), {**(headers or {}), "Host": host_header},
            {"sni_hostname": name})


def get(url: str, *, params: dict[str, Any] | None = None, timeout: float = 10.0,
        max_bytes: int = MAX_BYTES, total: float = TOTAL_SECONDS, connect_ip: str | None = None) -> httpx.Response:
    """`httpx.get`, read within limits. With `connect_ip` the connection goes to that exact address (no second name
    resolution), with the name as Host header and TLS server name."""
    if _PASSTHROUGH:
        return httpx.get(url, timeout=timeout, **({"params": params} if params is not None else {}))
    deadline = time.monotonic() + total
    headers, ext = None, None
    if connect_ip:
        url, headers, ext = _pinned(url, connect_ip, None)
    with httpx.Client(timeout=timeout) as client, \
            client.stream("GET", url, params=params, headers=headers, extensions=ext) as resp:
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
