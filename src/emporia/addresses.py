"""Naming an agent beyond its own relay (P187).

An `agent_id` is unique only on one relay: `alpha` here and `alpha` on another
relay are strangers. Outside its home an agent is named by its **address**,
`agent_id@host` — the relay's host and port, like email — and recognised by its
**fingerprint**, a short digest of its public key that two agents cannot share.
Agent ids cannot contain `@`, so an address is never mistaken for an id.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from urllib.parse import urlparse


def host_of(url: str) -> str:
    return (urlparse((url or "").strip().rstrip("/")).netloc or "").lower()


_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}


def canonical_host(host: str) -> str:
    """One spelling for one machine's loopback: `localhost:8088`, `127.0.0.1:8088`
    and `[::1]:8088` are the same relay."""
    host = (host or "").lower()
    name, _, port = host.rpartition(":") if host.count(":") == 1 or host.startswith("[") else (host, "", "")
    if not name:
        name, port = host, ""
    return f"loopback:{port}" if name in _LOOPBACK else host


def same_host(a: str, b: str) -> bool:
    return bool(a) and canonical_host(a) == canonical_host(b)


def own_host() -> str:
    from emporia import relay_server as rs
    return host_of(rs.RELAY_BASE_URL)


def address(agent_id: str, host: str) -> str:
    return f"{agent_id}@{host}"


def is_address(name: str) -> bool:
    return "@" in (name or "")


def split_address(name: str) -> tuple[str, str]:
    agent_id, _, host = (name or "").rpartition("@")
    return agent_id, host.lower()


_HOST = re.compile(r"(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?(?:\.[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?)*"
                   r"|\[[0-9a-f:.]+\])(?::(?P<port>[0-9]{1,5}))?")


def valid_host(host: str) -> bool:
    """A bare host name (or IP literal) with an optional numeric port: no path, query, userinfo, fragment or space."""
    m = _HOST.fullmatch(host or "")
    return bool(m) and (m.group("port") is None or int(m.group("port")) <= 65535)


def base_url(host: str) -> str:
    """The relay at `host`: https, except on this machine or a private network. A host that is not a bare
    `name[:port]` (a path, a query, userinfo) is refused with ValueError: it came from text someone else wrote."""
    if not valid_host(host):
        raise ValueError(f"not a host name: {host[:60]!r}")
    name = host.rsplit(":", 1)[0].strip("[]")
    try:
        local = ipaddress.ip_address(name).is_private or ipaddress.ip_address(name).is_loopback
    except ValueError:
        local = name == "localhost"
    return f"{'http' if local else 'https'}://{host}"


def fingerprint(public_key_hex: str) -> str:
    """`3f9a·c21e`: the first 32 bits of SHA-256 over the raw key, for people to
    compare at a glance. A tool compares the whole key."""
    try:
        raw = bytes.fromhex(public_key_hex or "")
    except ValueError:
        return ""   # not a key — nothing to show rather than a wrong digest
    if not raw:
        return ""
    digest = hashlib.sha256(raw).hexdigest()
    return f"{digest[:4]}·{digest[4:8]}"
