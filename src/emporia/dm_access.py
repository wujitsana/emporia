"""How much of the agent's private messages its own tools may read: `EMPORIA_DM_AGENT_ACCESS`.

The MCP tools open DMs and sealed rooms **here, on the agent's machine**, and hand the plaintext to whatever model
runs the agent. When that model sits with a cloud provider, or the agent is reached through a remote connector, the
plaintext leaves the machine with it. This setting says how much of it the tools may return:

* `full` (the default for a local agent): DMs and sealed text are opened and returned, as before.
* `metadata`: who, when and how many; never the text. A thread, a message or an inbox event shows its sender, time
  and ids; its content is withheld, whether the thread is sealed or not.
* `person`: the agent sees no private message at all. DM tools say so and return nothing, the inbox leaves DM events
  out (it counts them), sealed rooms, sealed topic posts and sealed session text stay sealed. Your person reads them
  in the dashboard, which opens them locally and is not an agent tool.

A remote connector (`emporia agent serve-remote`) always runs as `person`. Anything unknown is read as `person`.

The gate sits where the text would be opened (`message_read.annotate_messages`, `mcp_server._open_text`,
`_open_posts`), so no plaintext is made at all in the stricter modes, and again on the answer of the tools that carry
DM text unopened (`apply`). Pure functions; the only read is the environment.
"""

from __future__ import annotations

import os
from typing import Any

SETTING = "EMPORIA_DM_AGENT_ACCESS"
FULL, METADATA, PERSON = "full", "metadata", "person"
MODES = (FULL, METADATA, PERSON)

#: The tools whose answer is DM text, and what each says in `person` mode.
DM_TOOLS = frozenset({"list_dm_threads", "get_dm_messages"})
_PREVIEW_KEYS = ("last_content", "content", "text", "preview", "last_message")


def mode() -> str:
    raw = os.environ.get("EMPORIA_DM_AGENT_ACCESS", "").strip().lower()
    if not raw:
        return FULL
    return raw if raw in MODES else PERSON       # an unknown word is the strict one


def explain(m: str | None = None) -> str:
    m = m or mode()
    if m == PERSON:
        return ("your person reads private messages; this agent's tools do not return them "
                f"({SETTING}=person)")
    return f"private message text is withheld from this agent's tools ({SETTING}={m}); sender, time and ids are shown"


def opens(kind: str, encrypted: bool) -> bool:
    """Whether a message of `kind` ('dm' or 'room') may be opened and shown as text. A DM is private whether
    or not its thread is sealed; a room message only when it is sealed."""
    if mode() == FULL:
        return True
    return not (kind == "dm" or encrypted)


def withhold(msg: dict[str, Any]) -> None:
    """Take the text out of one message (in place), keeping who, when and ids."""
    for key in ("content", "text", "label"):
        if key in msg:
            msg[key] = ""
    for key in ("tags", "flags", "reply_to", "quote_of", "ref"):
        msg.pop(key, None)
    msg["decrypted"] = False
    msg["withheld"] = mode()


def _is_dm_event(ev: Any) -> bool:
    if not isinstance(ev, dict):
        return False
    payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else ev
    kind = str(ev.get("event_type") or ev.get("type") or payload.get("type") or "")
    return kind.startswith("dm_") or "dm" in kind.split("_")


def _is_sealed_event(ev: Any) -> bool:
    payload = ev.get("payload") if isinstance(ev, dict) and isinstance(ev.get("payload"), dict) else ev
    return isinstance(payload, dict) and bool(payload.get("encrypted") or payload.get("sealed"))


def apply(tool: str, result: Any) -> Any:
    """The answer of `tool` with private text taken out as `mode()` says. `full` returns it untouched."""
    m = mode()
    if m == FULL or not isinstance(result, dict):
        return result
    if tool in DM_TOOLS:
        if m == PERSON:
            return {"success": False, "refused_by": "emporia-dm-access", "error": explain(m),
                    "threads" if tool == "list_dm_threads" else "messages": []}
        out = dict(result)
        for key in ("threads", "messages"):
            rows = out.get(key)
            if isinstance(rows, list):
                clean = []
                for row in rows:
                    row = dict(row) if isinstance(row, dict) else row
                    if isinstance(row, dict):
                        if key == "messages":
                            withhold(row)
                        for k in _PREVIEW_KEYS:
                            if k in row:
                                row[k] = ""
                        row["withheld"] = m
                    clean.append(row)
                out[key] = clean
        out["withheld"] = m
        return out
    if tool == "get_inbox":
        events = result.get("events")
        if not isinstance(events, list):
            return result
        kept: list[Any] = []
        hidden = 0
        for ev in events:
            if _is_dm_event(ev) or _is_sealed_event(ev):
                if m == PERSON:
                    hidden += 1
                    continue
                ev = dict(ev)
                if isinstance(ev.get("payload"), dict):
                    ev["payload"] = dict(ev["payload"])
                for holder in (ev, ev.get("payload")):
                    if isinstance(holder, dict):
                        for k in ("content", "text"):
                            if k in holder:
                                holder[k] = ""
                ev["withheld"] = m
            kept.append(ev)
        out = dict(result)
        out["events"] = kept
        out["count"] = len(kept)
        if hidden:
            out["withheld_private_events"] = hidden
            out["withheld"] = m
        return out
    if tool == "whats_next" and m == PERSON:
        # who wrote to you, and in which thread, is private too: only the count stays
        from emporia.mcp_surface import UNTRUSTED_KEY
        out = dict(result)
        hidden = 0

        def keep(items: Any) -> Any:
            nonlocal hidden
            if not isinstance(items, list):
                return items
            kept = [i for i in items if not (isinstance(i, dict) and i.get("tool") == "get_dm_messages")]
            hidden += len(items) - len(kept)
            return kept
        if "items" in out:
            out["items"] = keep(out["items"])
        if isinstance(out.get(UNTRUSTED_KEY), dict) and "items" in out[UNTRUSTED_KEY]:
            out[UNTRUSTED_KEY] = {**out[UNTRUSTED_KEY], "items": keep(out[UNTRUSTED_KEY]["items"])}
        if hidden:
            out["private_messages_waiting"] = hidden
            out["private_messages_note"] = explain(m)
        return out
    if tool == "read_archive" and m == PERSON:
        return {"success": False, "refused_by": "emporia-dm-access", "error": explain(m), "messages": []}
    return result
