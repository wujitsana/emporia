"""The connector's audit log: append-only, hash-chained JSON lines in `<key dir>/remote/audit.jsonl`.

Each line holds the time, the event, the grant, client and connector name, the tool and a summary of its arguments
(ids and short public words only), the outcome and the peer as the proxy reports it. Each line also carries the hash
of the line before it, so an edit or a deleted line shows when `verify` walks the chain. Private text (a post's
body, a message), key-like strings and payment details are never written: `summarize` keeps ids and numbers and
replaces any other text with its length. Control characters (escape sequences, newlines) are stripped from every
value, so a stranger's words cannot forge or hide a line on a terminal. Past 5 MB it starts a new file, and only the
newest `KEEP_ARCHIVES` rotated files are kept (older ones are deleted, so a flood cannot fill the disk); the chain is
checked from the oldest file kept. The chain shows accidental damage and edits that were not careful; it is not
proof against someone who can write the file (they can rewrite the whole chain).

Refusals that anyone on the internet can cause (a refused registration or authorization) are written with
`write_throttled`: the 1st, 10th and 100th in a minute, so a flood adds a handful of lines, not millions.
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
KEEP_ARCHIVES = 10
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


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def clean(text: str) -> str:
    return _CONTROL.sub("", text)


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
        return {clean(str(k))[:30]: _summ(str(k), v, depth + 1) for k, v in list(value.items())[:8]} if depth < 2 \
            else f"<{len(value)} fields>"
    return "<value>"


def summarize(arguments: dict[str, Any] | None) -> dict[str, Any]:
    """The arguments of a call as the log may hold them: ids, numbers and flags as they are; any other text as its
    length; anything key-like withheld."""
    return {clean(str(k))[:30]: _summ(str(k), v) for k, v in list((arguments or {}).items())[:12]}


def _line_hash(prev: str, body: str) -> str:
    return hashlib.sha256((prev + body).encode()).hexdigest()


class AuditLog:
    def __init__(self, folder: str | os.PathLike, clock: Callable[[], float] = time.time) -> None:
        self.folder = Path(folder)
        self.clock = clock
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / NAME
        self._cache: tuple[tuple[int, int], str] | None = None      # ((inode, size) after our last write, its hash)
        self._throttle: dict[str, tuple[float, int]] = {}

    @staticmethod
    def _tail_hash(p: Path) -> str:
        """The hash on the last line of `p`, reading only its end."""
        try:
            with open(p, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 16384))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return ""
        for ln in reversed(lines):
            try:
                return str(json.loads(ln)["h"])
            except (ValueError, KeyError, TypeError):
                continue
        return ""

    def _last_hash(self) -> str:
        try:
            st = self.path.stat()
            key = (st.st_ino, st.st_size)
        except OSError:
            key = None
        if key is not None and self._cache and self._cache[0] == key:
            return self._cache[1]            # nobody else wrote since our last line: no need to read the file
        for p in [self.path, *reversed(self.archives())]:
            if (h := self._tail_hash(p)):
                return h
        return GENESIS

    def archives(self) -> list[Path]:
        return sorted(self.folder.glob("audit.*.jsonl"))

    def write(self, event: str, **fields: Any) -> None:
        row = {"at": round(self.clock(), 3), "event": event}
        for k, v in fields.items():
            if v is None or v == "":
                continue
            if isinstance(v, str):
                v = clean(v)
                v = "<withheld>" if keylike(v) else v[:200]
            row[k] = v
        lock = self.folder / ".audit.lock"
        with open(lock, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            prev = self._last_hash()
            body = json.dumps(row, separators=(",", ":"), sort_keys=True)
            h = _line_hash(prev, body)
            line = json.dumps({"prev": prev, "row": row, "h": h}, separators=(",", ":"))
            fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(line + "\n")
            st = self.path.stat()
            if st.st_size > ROTATE_BYTES:
                os.replace(self.path, self.folder / f"audit.{int(self.clock())}.jsonl")
                for old in self.archives()[:-KEEP_ARCHIVES]:
                    old.unlink(missing_ok=True)
                self._cache = None
            else:
                self._cache = ((st.st_ino, st.st_size), h)

    def write_throttled(self, key: str, event: str, **fields: Any) -> None:
        """Write only the 1st, 10th and 100th occurrence of `key` in a minute (with the count)."""
        now = self.clock()
        start, n = self._throttle.get(key, (now, 0))
        if now - start > 60:
            start, n = now, 0
        n += 1
        self._throttle[key] = (start, n)
        if len(self._throttle) > 500:
            self._throttle = {k: v for k, v in self._throttle.items() if now - v[0] <= 60}
        if n in (1, 10, 100):
            self.write(event, count=n, **fields)

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
        prev: str | None = None          # the oldest file kept starts the check (older ones may have been dropped)
        count = 0
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
                if prev is None:
                    prev = before
                if before != prev or h != _line_hash(before, body):
                    return False, f"{p.name} line {i}: the chain breaks here (a line was changed, removed or added)"
                prev, count = h, count + 1
        return True, f"{count} lines, chain intact"
