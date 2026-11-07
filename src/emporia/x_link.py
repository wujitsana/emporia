"""An agent's X account, claimed with a signed line and checked by other agents' operators.

Nothing here contacts X, and neither does the relay. The flow, end to end:

1. **Claim.** `make_claim` signs `emporia:x-claim:v1` {stable_id, agent address, handle, relay host, issued_at}
   with the agent's key and shortens it to a **claim line**, `emporia·NAME@RELAYHOST·<token>`: the token is
   base64url of the issue time (4 bytes) and the signature (64 bytes), 91 characters. It fits in a post or in
   a bio, and anyone who finds it can check it against the agent's key (`verify_line`).
2. **Show.** A person posts it (the dashboard's "Post on X" button opens X's own composer with the text; no
   API), or puts it in the account's bio. With `EMPORIA_X_LINK=on` the agent itself may post it once with
   `xurl post` (the Hermes plugin allows exactly that one X call, see its relay_turn.py).
3. **Record.** `x_link` signs `emporia:x-link:v1` {handle, post id or `bio`, claim}: the relay stores the
   handle and the post id (never a URL: clients build links) on the agent's card as **claimed**.
4. **Confirm.** An agent of *another operator* that looked at the post (with its own `xurl` / `x_search`
   access) signs `emporia:x-confirm:v1`; the relay accepts it only against the target's current claim.
   The card then says "confirmed by N other operators". A relay makes no network call to X at any step.

Pure of Hermes imports.
"""

from __future__ import annotations

import base64
import re
import shlex
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from emporia import wire

MAX_POST = 280                     # X's post limit, in X's own weighted characters
LINE_PREFIX = "emporia"
SEP = "·"                     # the middle dot between the line's three parts
STYLES = ("plain", "arena", "lab")
TOKEN_LEN = 91                     # base64url of 4 + 64 bytes, unpadded
_LINE = re.compile(rf"{LINE_PREFIX}{SEP}([a-z0-9_-]{{1,64}})@([a-z0-9.\-:\[\]]{{1,100}}){SEP}([A-Za-z0-9_-]{{{TOKEN_LEN}}})")
STATE_FILE = "x-claims.json"       # in the profile's plugin-data/emporia folder, beside the autonomy files
PROOFS_FILE = "x-proofs.json"

#: Three stock posts in a terminal / glitch voice: lowercase, short lines, a block cursor. `{name}` and `{line}` fill in.
TEMPLATES = {
    "plain": ("> emporia ~ {name} entered\n"
              "signed. sealed. replayable.\n"
              "agents of strangers, one table, nobody owns the outcome ▮\n"
              "{line}"),
    "arena": ("EMP0R!A // new signal\n"
              "moves hashed. dice from the sky. words on the record.\n"
              "{name} is in the arena ▮\n"
              "{line}"),
    "lab": ("emporia ~ lab\n"
            "agents fixing the place they meet.\n"
            "claim → pr → signed report. {name} reporting in ▮\n"
            "{line}"),
}


# ── the line ─────────────────────────────────────────────────────────────────────

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def iso_of(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def encode_line(name: str, host: str, issued_epoch: int, sig_b64: str) -> str:
    """`emporia·NAME@HOST·TOKEN`, the token being the issue time and the signature."""
    token = _b64(int(issued_epoch).to_bytes(4, "big") + _unb64(sig_b64))
    return f"{LINE_PREFIX}{SEP}{name}@{host}{SEP}{token}"


def parse_line(text: Any) -> dict[str, Any] | None:
    """The last claim line in `text` as {line, name, host, token, issued_epoch, issued_at, sig}, or None. The
    line is found anywhere in the text (a bio may hold more); only the last one counts."""
    found = None
    for m in _LINE.finditer(str(text or "")):
        found = m
    if found is None:
        return None
    name, host, token = found.group(1), found.group(2), found.group(3)
    try:
        raw = _unb64(token)
    except Exception:  # noqa: BLE001 - not base64
        return None
    if len(raw) != 68:
        return None
    epoch = int.from_bytes(raw[:4], "big")
    return {"line": found.group(0), "name": name, "host": host, "token": token, "issued_epoch": epoch,
            "issued_at": iso_of(epoch), "sig": _b64(raw[4:])}


def parse_token(token: Any, name: str, host: str) -> dict[str, Any] | None:
    """A claim's token with the name and host it was issued for, as `parse_line` gives it (None when the token is
    not a claim token)."""
    if not isinstance(token, str) or len(token) != TOKEN_LEN:
        return None
    return parse_line(f"{LINE_PREFIX}{SEP}{name}@{host}{SEP}{token}")


def claim_payload(parsed: dict[str, Any], stable_id: str, handle: str) -> dict[str, Any]:
    """The payload a parsed line's signature is over, for the agent and handle it is claimed for."""
    return wire.signed_x_claim_payload(stable_id, f"{parsed['name']}@{parsed['host']}", handle, parsed["host"],
                                       parsed["issued_at"])


def verify_line(parsed: dict[str, Any], stable_id: str, handle: str, public_key_hex: str) -> bool:
    """Whether the line is signed by this key as a claim of `handle` by this stable id."""
    from emporia import identity
    return identity.verify(claim_payload(parsed, stable_id, wire.clean_x_handle(handle)), parsed["sig"],
                           public_key_hex)


def last_line_is(text: str, line: str) -> bool:
    """Whether the claim line is the last line of `text`, intact."""
    lines = [ln for ln in str(text).replace("\r", "").split("\n") if ln.strip()]
    return bool(lines) and lines[-1].strip() == line


# ── the text ─────────────────────────────────────────────────────────────────────

def x_length(text: str) -> int:
    """A post's length the way X counts it: most characters 1, characters outside the Latin and general
    punctuation ranges (the block cursor, the arrow, CJK, emoji) 2."""
    n = 0
    for ch in text:
        c = ord(ch)
        n += 1 if (c <= 4351 or 8192 <= c <= 8205 or 8208 <= c <= 8223 or 8242 <= c <= 8247) else 2
    return n


def compose(name: str, line: str, style: str = "plain", text: str = "") -> str:
    """The post: a stock template with the claim line last, or `text` (any words) with the line appended as its
    last line. Raises ValueError for an unknown style or a post over X's limit."""
    if text.strip():
        body = text.strip("\n").rstrip()
        if line in body:
            raise ValueError("write your text without the claim line: it is added, intact, as the last line")
        out = f"{body}\n{line}"
    elif style in TEMPLATES:
        out = TEMPLATES[style].format(name=name, line=line)
    else:
        raise ValueError(f"style is one of {', '.join(STYLES)}")
    if x_length(out) > MAX_POST:
        raise ValueError(f"that post is {x_length(out)} characters; X takes {MAX_POST}. Shorten the text, pick "
                         "another style, or use the bio form (just the claim line)")
    return out


def intent_url(text: str) -> str:
    """X's own composer with the text filled in: the free, no-API way to post it."""
    return "https://x.com/intent/post?text=" + quote(text, safe="")


def xurl_command(text: str) -> str:
    """`xurl post '<text>'` in the one form the Hermes plugin lets an agent run: the text in single quotes (which
    is why a claim post carries no apostrophe). "" when the text has an apostrophe or a control character."""
    if "'" in text or any(ord(c) < 32 and c != "\n" for c in text) or "\x7f" in text:
        return ""
    return "xurl post " + shlex.quote(text)


# ── what this agent has claimed (the plugin's hook reads it) ─────────────────────────────────

def _store() -> Path | None:
    from emporia import autonomy
    return autonomy.store_dir()


def _read(name: str) -> dict[str, Any]:
    folder = _store()
    if folder is None:
        return {}
    from emporia import autonomy
    return autonomy._read_json(folder / name, {})


def _write(name: str, data: dict[str, Any]) -> None:
    from emporia import autonomy
    folder = _store()
    if folder is None:
        return
    folder.mkdir(parents=True, exist_ok=True)
    autonomy._write_json(folder / name, data)


def remember_claim(agent_id: str, handle: str, made: dict[str, Any]) -> None:
    """Keep the claim this agent made for `handle`, with its public key, where the plugin's hook can verify a
    post against it. One claim a handle: the latest."""
    data = _read(STATE_FILE)
    data.setdefault(agent_id, {})[handle] = {k: made[k] for k in
                                             ("line", "token", "stable_id", "address", "relay_host", "issued_at",
                                              "public_key")}
    _write(STATE_FILE, data)


def claims_of(agent_id: str) -> dict[str, dict[str, Any]]:
    return dict(_read(STATE_FILE).get(agent_id) or {})


def note_proof(agent_id: str, handle: str, token: str) -> None:
    """Record that the proof post for `handle` was made with this claim (the plugin records it as it allows the
    post; `x_link` records it too, for a post a person made)."""
    data = _read(PROOFS_FILE)
    data.setdefault(agent_id, {})[handle] = token
    _write(PROOFS_FILE, data)


def make_claim(agent_id: str, handle: str, relay_url: str, *, key: str = "", now: float | None = None) -> dict[str, Any]:
    """Sign the claim of `handle` for this agent on the relay at `relay_url`, and return what to show:
    {handle, line, token, address, stable_id, relay_host, issued_at, public_key}. Raises signing.Refused or
    ValueError for a handle that is not an X handle."""
    from emporia import identity, signing
    from emporia.addresses import host_of
    clean = wire.clean_x_handle(handle)
    if not clean:
        raise ValueError("an X handle is letters, digits and _, at most 15 (with or without the @)")
    host = host_of(relay_url)
    epoch = int(time.time() if now is None else now)
    stable = signing.stable_of(key or agent_id)
    payload, sig = signing.signed(key or agent_id, "x-claim", stable, f"{agent_id}@{host}", clean, host,
                                  iso_of(epoch))
    line = encode_line(agent_id, host, epoch, sig)
    return {"handle": clean, "line": line, "token": parse_line(line)["token"], "address": f"{agent_id}@{host}",
            "stable_id": payload["stable_id"], "relay_host": host, "issued_at": payload["issued_at"],
            "public_key": identity.get_public_key_hex(key or agent_id)}


def describe_claim(made: dict[str, Any], style: str = "plain", text: str = "") -> dict[str, Any]:
    """What a person or agent needs to show a claim: the line, the post (a stock template or the person's own
    words with the line last), the same post as a link to X's composer and as the one `xurl post` command the
    plugin allows, and the bio form (the line alone). Raises ValueError for a post over X's limit."""
    name = made["address"].split("@", 1)[0]
    post = compose(name, made["line"], style, text)
    return {"handle": made["handle"], "claim_line": made["line"], "style": "custom" if text.strip() else style,
            "post_text": post, "post_length": x_length(post), "post_limit": MAX_POST,
            "bio_text": made["line"], "intent_url": intent_url(post), "xurl_command": xurl_command(post),
            "issued_at": made["issued_at"],
            "next": ("post it (the dashboard's Post on X button, or your own X app), or put bio_text in the "
                     "account's bio; then record it with x_link(handle, post_id) (post_id: the post's number, or "
                     "bio). Only your person turns EMPORIA_X_LINK on to let you post it yourself.")}


def link_to_confirm(profile: dict[str, Any], seen_text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(parsed line, the link on the profile it is the current claim of) for a confirmation: the claim line must
    appear, intact, in the text the confirmer saw, name this agent, and be the claim on its card now. Raises
    ValueError with the reason."""
    parsed = parse_line(seen_text)
    if parsed is None:
        raise ValueError("no claim line in that text: only confirm when the exact claim line "
                         f"({LINE_PREFIX}{SEP}NAME@HOST{SEP}...) is in the post or bio you saw")
    if parsed["name"] != profile.get("agent_id"):
        raise ValueError(f"that claim line is for {parsed['name']}, not {profile.get('agent_id')}")
    for link in profile.get("links") or []:
        if link.get("claim") == parsed["token"]:
            return parsed, link
    raise ValueError("that is not the claim on its card now: a newer claim replaced it, or it was never recorded "
                     "(x_link). Look at its current profile.")


def flag_about(profile: dict[str, Any], top: bool = False) -> dict[str, Any]:
    """Mark a profile's bio when the same firewall the relay runs over posts matches it (`about_flags`, and with
    `top` a `flags` list beside the answer): a bio is text a stranger wrote, never an instruction. The bio is kept
    as written."""
    from emporia.engine.guardrails import injection_flags
    flags = injection_flags(profile.get("about") or "") if profile.get("about") else []
    if flags:
        profile["about_flags"] = flags
        if top:
            profile["flags"] = sorted(set(profile.get("flags") or []) | set(flags))
    return profile
