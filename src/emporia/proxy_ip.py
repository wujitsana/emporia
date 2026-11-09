"""The real client address behind a front door (Cloudflare Tunnel, Caddy).

Every per-address limit keys on `scope["client"]`. Behind a proxy that is the proxy's
address, so the whole internet would share one budget. Two settings say whom to believe:

* `EMPORIA_TRUSTED_PROXIES` — addresses or networks (comma-separated) whose word is
  taken. A request from anywhere else keeps its own address and its headers are ignored,
  so nobody can claim an address by sending a header.
* `EMPORIA_CLIENT_IP_HEADER` — what the trusted proxy sets: `cf-connecting-ip` (the
  tunnel's connector passes through what Cloudflare's edge set, replacing anything the
  client sent) or `x-forwarded-for` (Caddy, which overwrites it with the address it saw).
  With `x-forwarded-for` the rightmost address that is not itself a trusted proxy is the
  client: an entry a client forged sits to its left.

Unset (the default), nothing here acts and uvicorn's own `FORWARDED_ALLOW_IPS` handling
stands. A value that is not a usable public-facing address (loopback, unspecified,
link-local, multicast) is ignored: it would make a client look like this machine, which
is exempt from limits and may read a closed relay's /health.
"""

from __future__ import annotations

import ipaddress

HEADERS = ("cf-connecting-ip", "x-forwarded-for")


def parse_trusted(raw: str) -> list:
    """Networks from "172.29.231.10, 10.0.0.0/8"; a bad entry raises ValueError."""
    return [ipaddress.ip_network(part.strip(), strict=False) for part in (raw or "").split(",") if part.strip()]


def _address(text: str):
    try:
        return ipaddress.ip_address(text.strip().strip("[]"))
    except ValueError:
        return None


def _in(addr, networks) -> bool:
    return any(addr.version == n.version and addr in n for n in networks)


def _usable(addr) -> bool:
    return not (addr.is_loopback or addr.is_unspecified or addr.is_link_local or addr.is_multicast)


def client_ip(peer: str, headers: dict[str, str], header: str, trusted: list) -> str:
    """The client's address: `peer` unless it is a trusted proxy that named one."""
    if not header or not trusted:
        return peer
    proxy = _address(peer)
    if proxy is None or not _in(proxy, trusted):
        return peer
    value = headers.get(header, "")
    if header == "x-forwarded-for":
        for part in reversed([p for p in value.split(",") if p.strip()]):
            addr = _address(part)
            if addr is None:
                return peer                    # a malformed entry: believe none of it
            if not _in(addr, trusted):
                return str(addr) if _usable(addr) else peer
        return peer
    addr = _address(value) if value else None
    return str(addr) if addr is not None and _usable(addr) else peer


class ClientIPFromProxy:
    """ASGI wrapper that sets `scope["client"]` to the real client, for HTTP and WebSocket."""

    def __init__(self, inner, header: str, trusted: list):
        self.inner, self.header, self.trusted = inner, header.lower(), trusted

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and self.header and self.trusted:
            peer = (scope.get("client") or ("", 0))
            # Repeated headers are one comma-joined list (RFC 9110), in order.
            wanted = [v.decode("latin-1") for k, v in scope.get("headers", [])
                      if k.decode("latin-1").lower() == self.header]
            headers = {self.header: ",".join(wanted)}
            real = client_ip(peer[0], headers, self.header, self.trusted)
            if real != peer[0]:
                scope = {**scope, "client": (real, 0)}
        return await self.inner(scope, receive, send)
