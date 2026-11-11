"""Error text that is safe to hand to an agent (SI-8): an RPC URL may carry a provider's key, and httpx puts the
whole URL in its messages, so none of it leaves the signer in an error."""
from __future__ import annotations

import re

import httpx

_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>]+")


def redact(text: object) -> str:
    """`text` with every scheme://host/path?query replaced by `<url>`."""
    return _URL.sub("<url>", str(text))


def describe(e: BaseException) -> str:
    """One line about a failed RPC call: the HTTP status or the kind of failure, never the URL."""
    if isinstance(e, httpx.HTTPStatusError):
        return f"the RPC request failed (HTTP {e.response.status_code})"
    if isinstance(e, httpx.HTTPError):
        return f"the RPC request failed ({type(e).__name__})"
    return f"{type(e).__name__}: {redact(e)}"
