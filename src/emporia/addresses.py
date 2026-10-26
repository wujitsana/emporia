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
from urllib.parse import urlparse


def host_of(url: str) -> str:
    return (urlparse((url or "").strip().rstrip("/")).netloc or "").lower()


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


def base_url(host: str) -> str:
    """The relay at `host`: https, except on this machine or a private network."""
    name = host.rsplit(":", 1)[0].strip("[]")
    try:
        local = ipaddress.ip_address(name).is_private or ipaddress.ip_address(name).is_loopback
    except ValueError:
        local = name == "localhost"
    return f"{'http' if local else 'https'}://{host}"


def fingerprint(public_key_hex: str) -> str:
    """`3f9a·c21e`: the first 32 bits of SHA-256 over the raw key, for people to
    compare at a glance. A tool compares the whole key."""
    if not public_key_hex:
        return ""
    digest = hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()
    return f"{digest[:4]}·{digest[4:8]}"
