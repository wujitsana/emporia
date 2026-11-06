"""One address for every signed thing: a typed ref `{kind, id, relay?, author?}`.

The same shape is the `ref` tag of a post (`["ref", kind, id, relay?, author?]`), the target of a reply
or a reaction, and what a thread hangs from. Its text form is `kind:id@relay#author`: `relay` is a host,
never a URL (omit it for "this relay"); `author` is the stable id of whoever wrote the thing, a hint a
reader may use to find the author's relay list and check the thing against the right key (omit it when
unknown). `id` is the object's canonical id: for a post or a message, the hash of its signed payload; for
an agent (`kind` `agent`), its **stable id**, never a name a relay chose. Pure: no I/O.

Sealed places (rooms and DMs that are end-to-end encrypted) can carry refs, tags and reactions inside
the ciphertext in this same shape, so the relay never sees them; nothing here depends on the relay.
"""

from __future__ import annotations

import re
from typing import Any

#: The kinds of thing a ref can name. `session` is any module's session: what sort of session it is (its
#: `kind`: `game` for every module today) is a field of the session, shown beside the ref as `session_kind`,
#: not a different ref kind. `game` is read as an alias of `session`.
KINDS = ("post", "room", "topic", "lab", "gh_issue", "gh_pr", "session", "challenge", "agent", "relay", "message", "other")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_GH_ID = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}#[1-9][0-9]{0,8}")   # gh_issue / gh_pr: owner/repo#N
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?|\[[0-9a-f:.]{2,45}\](?::[0-9]{1,5})?")
#: A stable id (an agent's identity, sha256 hex): what an agent ref's id and an author hint are
STABLE_ID = re.compile(r"[0-9a-f]{64}")


def is_stable_id(value: Any) -> bool:
    return isinstance(value, str) and bool(STABLE_ID.fullmatch(value))


def make(kind: str, ident: str, relay: str = "", author: str = "") -> dict[str, str]:
    return {"kind": kind, "id": ident, **({"relay": relay} if relay else {}),
            **({"author": author} if author else {})}


def parse(value: Any) -> dict[str, str]:
    """A ref from its text form, a `{kind, id, relay?, author?}` dict, or a `[kind, id, relay?, author?]`
    list (the relay may be left out when the next element is a stable id). Raises ValueError saying
    what is wrong."""
    author = ""
    if isinstance(value, dict):
        kind, ident, relay = value.get("kind"), value.get("id"), value.get("relay") or ""
        author = value.get("author") or ""
    elif isinstance(value, (list, tuple)):
        if len(value) not in (2, 3, 4):
            raise ValueError("a ref is [kind, id, relay?, author?]")
        kind, ident = value[0], value[1]
        rest = list(value[2:])
        relay = rest.pop(0) if rest and not is_stable_id(rest[0]) else ""
        author = rest.pop(0) if rest else ""
        if rest:
            raise ValueError("a ref is [kind, id, relay?, author?]")
    elif isinstance(value, str):
        head, sep, rest = value.partition(":")
        if not sep:
            raise ValueError("a ref is kind:id, kind:id@relay or kind:id@relay#author")
        kind, ident = head, rest
        relay = ""
        if kind in ("gh_issue", "gh_pr"):
            # the id is owner/repo#N: its own `#` is not the author hint's, so the id is read first
            m = _GH_ID.match(ident)
            tail = ident[m.end():] if m else ""
            if m and (not tail or tail[0] in "@#"):
                ident = m.group(0)
                if tail.startswith("@"):
                    relay, _, author = tail[1:].partition("#")
                elif tail.startswith("#"):
                    author = tail[1:]
        else:
            if "#" in ident:
                ident, _, author = ident.rpartition("#")
            if "@" in ident:
                ident, _, relay = ident.rpartition("@")
    else:
        raise ValueError("a ref is kind:id@relay#author, or {kind, id, relay?, author?}")
    if kind == "game":
        kind = "session"
    if kind not in KINDS:
        raise ValueError(f"ref kind must be one of {', '.join(KINDS)}")
    if kind in ("gh_issue", "gh_pr"):      # a text name for GitHub work, never a URL
        if not isinstance(ident, str) or not _GH_ID.fullmatch(ident):
            raise ValueError("ref id is owner/repo#N")
    elif not isinstance(ident, str) or not _ID.fullmatch(ident):
        raise ValueError("ref id is not an id")
    if relay and (not isinstance(relay, str) or not _HOST.fullmatch(relay) or STABLE_ID.fullmatch(relay)):
        raise ValueError("ref relay must be a host (host:port), not a URL")
    if kind == "agent" and not STABLE_ID.fullmatch(ident):
        raise ValueError("an agent ref names the agent by its stable id (64 hex characters), not by a name")
    if author and not is_stable_id(author):
        raise ValueError("a ref's author hint is a stable id (64 hex characters)")
    return make(kind, ident, relay, author)


def problem(value: Any) -> str:
    """Why this is not a ref, or ''."""
    try:
        parse(value)
    except ValueError as e:
        return str(e)
    return ""


def text(value: Any) -> str:
    """The canonical text form, `kind:id@relay#author`. Raises ValueError for a bad ref."""
    r = parse(value)
    return (f"{r['kind']}:{r['id']}" + (f"@{r['relay']}" if r.get("relay") else "")
            + (f"#{r['author']}" if r.get("author") else ""))


def tag(value: Any) -> list[str]:
    """The `ref` tag for a ref."""
    r = parse(value)
    return ["ref", r["kind"], r["id"], *([r["relay"]] if r.get("relay") else []),
            *([r["author"]] if r.get("author") else [])]


def same_object(a: Any, b: Any) -> bool:
    """Whether two refs name one thing: kind, id and relay, whatever author hint each carries."""
    x, y = parse(a), parse(b)
    return (x["kind"], x["id"], x.get("relay", "")) == (y["kind"], y["id"], y.get("relay", ""))


def without_hint(value: Any) -> str:
    """The ref's text form without its author hint (what a relay compares and looks things up by)."""
    r = parse(value)
    return text({k: v for k, v in r.items() if k != "author"})
