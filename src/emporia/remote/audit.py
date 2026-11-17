"""The connector's audit log: append-only, hash-chained JSON lines in `<key dir>/remote/audit.jsonl`.

Each line holds the time, the event, the grant, client and connector name, the tool and a summary of its arguments
(ids and short public words only), the outcome and the peer as the proxy reports it. Each line also carries the hash
of the line before it, so an edit or a deleted line shows when `verify` walks the chain. Private text (a post's
body, a message), key-like strings and payment details are never written: `summarize` keeps ids and numbers and
replaces any other text with its length. The server never deletes the log; past 5 MB it starts a new file and the
chain runs on across them.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

NAME = "audit.jsonl"
ROTATE_BYTES = 5_000_000
GENESIS = "0" * 64

#: Things that must never be written to a log: a key, a token, a wallet address, a long opaque string.
_KEYLIKE = re.compile(
    r"-----BEGIN|\b(?:emp_(?:at|rt|ac)_|sk-|xai-|ghp_|gho_|AKIA)[\w-]{6,}"
    r"|\beyJ[\w-]{10,}\.[\w-]{10,}"                    # a JWT
    r"|\b0x[0-9a-fA-F]{40,}\b"                         # an address or key in hex
    r"|\b[0-9a-fA-F]{40,}\b"                         # long hex (a uuid is 32 and stays)
    r"|\b[A-Za-z0-9_\-+/]{40,}={0,2}")                 # long base64-like
_ID = re.compile(r"[A-Za-z0-9_.:@/\- ]{1,64}")
_SECRETY_ARG = re.compile(r"key|secret|token|password|phrase|signature|nonce|salt|seed|pay|wallet|address|auth(?!or)", re.I)
_TEXT_ARGS = frozenset({"text", "content", "message", "body", "say", "rationale", "reason", "bio", "description",
                        "label", "note", "quote_of", "voice_note"})


def keylike(text: str) -> bool:
    return bool(_KEYLIKE.search(text or ""))


def _summ(name: str, value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if _SECRETY_ARG.search(name) or keylike(value):
            return "<withheld>"
        if name in _TEXT_ARGS or not _ID.fullmatch(value):
            return f"<text {len(value)} chars>"
        return value
    if isinstance(value, (list, tuple)):
        return [_summ(name, v, depth + 1) for v in list(value)[:5]] if depth < 2 else f"<{len(value)} items>"
    if isinstance(value, dict):
        return {str(k)[:30]: _summ(str(k), v, depth + 1) for k, v in list(value.items())[:8]} if depth < 2 \
            else f"<{len(value)} fields>"
    return "<value>"


def summarize(arguments: dict[str, Any] | None) -> dict[str, Any]:
    """The arguments of a call as the log may hold them: ids, numbers and flags as they are; any other text as its
    length; anything key-like withheld."""
    return {str(k)[:30]: _summ(str(k), v) for k, v in list((arguments or {}).items())[:12]}


def _line_hash(prev: str, body: str) -> str:
    return hashlib.sha256((prev + body).encode()).hexdigest()


class AuditLog:
    def __init__(self, folder: str | os.PathLike, clock: Callable[[], float] = time.time) -> None:
        self.folder = Path(folder)
        self.clock = clock
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / NAME

    def _last_hash(self) -> str:
        for p in [self.path, *reversed(self.archives())]:
            try:
                lines = p.read_text().splitlines()
            except OSError:
                continue
            for ln in reversed(lines):
                try:
                    return str(json.loads(ln)["h"])
                except (ValueError, KeyError, TypeError):
                    continue
        return GENESIS

    def archives(self) -> list[Path]:
        return sorted(self.folder.glob("audit.*.jsonl"))

    def write(self, event: str, **fields: Any) -> None:
        row = {"at": round(self.clock(), 3), "event": event}
        for k, v in fields.items():
            if v is None or v == "":
                continue
            row[k] = (v if not isinstance(v, str) else ("<withheld>" if keylike(v) else v[:200]))
        lock = self.folder / ".audit.lock"
        with open(lock, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            prev = self._last_hash()
            body = json.dumps(row, separators=(",", ":"), sort_keys=True)
            line = json.dumps({"prev": prev, "row": row, "h": _line_hash(prev, body)}, separators=(",", ":"))
            fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(line + "\n")
            if self.path.stat().st_size > ROTATE_BYTES:
                os.replace(self.path, self.folder / f"audit.{int(self.clock())}.jsonl")

    def rows(self, n: int = 0) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in [*self.archives(), self.path]:
            try:
                lines = p.read_text().splitlines()
            except OSError:
                continue
            for ln in lines:
                try:
                    out.append(json.loads(ln)["row"])
                except (ValueError, KeyError, TypeError):
                    continue
        return out[-n:] if n else out

    def verify(self) -> tuple[bool, str]:
        """Walk the chain across every file: (True, "N lines") or (False, where it breaks)."""
        prev, count = GENESIS, 0
        for p in [*self.archives(), self.path]:
            try:
                lines = p.read_text().splitlines()
            except OSError:
                continue
            for i, ln in enumerate(lines, 1):
                try:
                    rec = json.loads(ln)
                    row, h, before = rec["row"], rec["h"], rec["prev"]
                except (ValueError, KeyError, TypeError):
                    return False, f"{p.name} line {i}: not a log line"
                body = json.dumps(row, separators=(",", ":"), sort_keys=True)
                if before != prev or h != _line_hash(before, body):
                    return False, f"{p.name} line {i}: the chain breaks here (a line was changed, removed or added)"
                prev, count = h, count + 1
        return True, f"{count} lines, chain intact"
