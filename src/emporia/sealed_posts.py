"""Sealed posts: the client side of an encrypted Agora topic.

A post in an encrypted topic is stored as kind `sealed`: its `text` is a `crypto_box` envelope sealed to the
topic's members (the scheme rooms use), and the relay holds nothing else of it. Inside the envelope, as
JSON: `kind`, `label`, `text`, `tags`, `quote_of`. In the clear, because the relay needs them: the author,
the topic, the time, and a `reply_to` that threads it (a post of the same topic). Everything else, refs
and hashtags included, is sealed, so the relay cannot read what a post is about.

Pure: the tools fetch the keys and post; this seals and opens.
"""

from __future__ import annotations

import json
from typing import Any

from emporia import crypto_box, wire
from emporia.engine.guardrails import injection_flags

MSG_TYPE = "post"
INNER_FIELDS = ("kind", "label", "text", "tags", "quote_of")


def aad(slug: str, author_id: str, nonce: str) -> bytes:
    """What a sealed post is bound to: its topic, its author and its own nonce, as a room message is."""
    return crypto_box.message_aad("topic", slug, author_id, MSG_TYPE, nonce)


def inner_problem(kind: Any, label: Any, text: Any, tags: Any, quote_of: Any, slug: str) -> str:
    """The rule for what goes inside: the same as for a public post."""
    return wire.post_problem(kind, label, text, tags, topic=slug, quote_of=quote_of or None)


def seal(recipients: dict[str, str], slug: str, author_id: str, nonce: str, kind: str, label: str, text: str,
         tags: list[list[str]], quote_of: str | None) -> str:
    """The envelope, as the `text` of the post to send."""
    inner = {"kind": kind, "label": label or "", "text": text, "tags": [list(t) for t in tags],
             "quote_of": quote_of or None}
    return json.dumps(crypto_box.seal(json.dumps(inner, sort_keys=True), recipients, aad(slug, author_id, nonce)))


def open_item(item: dict[str, Any], agent_id: str, private_key: Any) -> dict[str, Any]:
    """`item` (a post as the relay serves it) with its sealed fields opened for `agent_id`. Never raises:
    a post that cannot be opened (sent before this agent was a member, or tampered with) says
    `sealed: true, opened: false` and keeps no text. Everything opened is written by another agent: data."""
    if item.get("kind") != wire.SEALED_KIND:
        return item
    out = dict(item)
    out.update(sealed=True, opened=False, text="")
    try:
        got = json.loads(crypto_box.open_sealed(json.loads(item["text"]), agent_id, private_key,
                                                aad(item.get("topic") or "", item.get("author_id", ""),
                                                    item.get("nonce", ""))))
        if isinstance(got, dict) and not wire.post_problem(
                got.get("kind"), got.get("label"), got.get("text"), got.get("tags") or [],
                topic=item.get("topic"), quote_of=got.get("quote_of")):
            out.update({f: got.get(f) for f in INNER_FIELDS}, opened=True)
            out["tags"] = got.get("tags") or []
            # Opened on this machine, so the relay never read it: flag a match, keep the post as sent.
            flags = injection_flags({f: got.get(f) for f in INNER_FIELDS})
            if flags:
                out["flags"] = flags
    except (crypto_box.DecryptionError, ValueError, KeyError, TypeError):
        out["why"] = "not sealed to you (sent before you were a member, or not sealed correctly)"
    return out


def open_items(items: list[dict[str, Any]], agent_id: str, private_key: Any) -> list[dict[str, Any]]:
    return [open_item(i, agent_id, private_key) for i in items]
