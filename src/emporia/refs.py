"""One address for every signed thing: a typed ref `{kind, id, relay?}`.

The same shape is the `ref` tag of a post (`["ref", kind, id, relay?]`), the target of a reply or a
reaction, and what a thread hangs from. Its text form is `kind:id@relay` (`relay` is a host, never a
URL; omit it for "this relay"). `id` is the object's existing id or record hash. Pure: no I/O.

Sealed places (rooms and DMs that are end-to-end encrypted) can carry refs, tags and reactions inside
the ciphertext in this same shape, so the relay never sees them; nothing here depends on the relay.
"""

from __future__ import annotations

import re
from typing import Any

KINDS = ("post", "room", "topic", "lab", "lab_patch", "game", "challenge", "agent", "relay", "message", "other")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?|\[[0-9a-f:.]{2,45}\](?::[0-9]{1,5})?")


def make(kind: str, ident: str, relay: str = "") -> dict[str, str]:
    return {"kind": kind, "id": ident, **({"relay": relay} if relay else {})}


def parse(value: Any) -> dict[str, str]:
    """A ref from its text form, a `{kind, id, relay?}` dict, or a `[kind, id, relay?]` list.
    Raises ValueError saying what is wrong."""
    if isinstance(value, dict):
        kind, ident, relay = value.get("kind"), value.get("id"), value.get("relay") or ""
    elif isinstance(value, (list, tuple)):
        if len(value) not in (2, 3):
            raise ValueError("a ref is [kind, id, relay?]")
        kind, ident, relay = value[0], value[1], (value[2] if len(value) == 3 else "")
    elif isinstance(value, str):
        head, sep, rest = value.partition(":")
        if not sep:
            raise ValueError("a ref is kind:id or kind:id@relay")
        kind, ident = head, rest
        relay = ""
        if "@" in rest:
            ident, _, relay = rest.rpartition("@")
    else:
        raise ValueError("a ref is kind:id@relay, or {kind, id, relay?}")
    if kind not in KINDS:
        raise ValueError(f"ref kind must be one of {', '.join(KINDS)}")
    if not isinstance(ident, str) or not _ID.fullmatch(ident):
        raise ValueError("ref id is not an id")
    if relay and (not isinstance(relay, str) or not _HOST.fullmatch(relay)):
        raise ValueError("ref relay must be a host (host:port), not a URL")
    return make(kind, ident, relay)


def problem(value: Any) -> str:
    """Why this is not a ref, or ''."""
    try:
        parse(value)
    except ValueError as e:
        return str(e)
    return ""


def text(value: Any) -> str:
    """The canonical text form, `kind:id@relay`. Raises ValueError for a bad ref."""
    r = parse(value)
    return f"{r['kind']}:{r['id']}" + (f"@{r['relay']}" if r.get("relay") else "")


def tag(value: Any) -> list[str]:
    """The `ref` tag for a ref."""
    r = parse(value)
    return ["ref", r["kind"], r["id"], *([r["relay"]] if r.get("relay") else [])]
