"""The agent witnesses its home relay's transparency log on its own (2.1).

A log protects no one unless somebody keeps what the relay showed and checks that it only ever grew. The
agent's bridge is always on and needs no model, so it does that here: once a day (and a couple of minutes
after it starts) it runs `logcheck.verify_relay` for the home relay, in a thread, within a time limit. The
result is kept in the profile's plugin data (`plugin-data/emporia/log_witness.json`): a status, a size, an
epoch, a time and the problems in words. Never a key or a signature; the evidence stays in `log_states.json`
in the agent's keys folder, where `logcheck` has always put it.

* A relay that keeps no log (`off`) is its operator's choice, not a problem. A relay that cannot be reached is
  not a problem either; the check is tried again within the hour.
* A problem (the log shrank, two histories, a rewritten log, a key change that is not in it) is told to the
  agent once, as a notice with fixed text, and to the gateway's log. The same problem is not told twice.
* It contacts only the agent's own relay (its log, its key changes). It does not ask any other relay what it was
  shown: `compare_with=[]` is passed, so no linked relay is read. That comparison stays in the explicit
  `emporia log verify` command a person runs, and in the relays' own checks of each other. So this check does not
  by itself catch a relay showing different agents different logs.

`EMPORIA_LOG_WITNESS=off` (the person's, like `EMPORIA_AUTO_REVEAL`) turns it off.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SETTING = "EMPORIA_LOG_WITNESS"
OFF_VALUES = frozenset({"off", "0", "false", "no", "never"})
RECORD = "log_witness.json"
FIRST_CHECK_SECONDS = 120
CHECK_EVERY_SECONDS = 24 * 3600
RETRY_SECONDS = 3600          # after a relay that did not answer
TIME_LIMIT_SECONDS = 180      # the bridge stops waiting for one check after this
KEEP_NOTICED = 20

_TAIL = (" Evidence is kept in log_states.json in the agent's keys folder. "
         "Run `emporia log verify {name}` for details.")
NOTICES = {
    "rewritten": "Your relay's transparency log did not check out: it may have rewritten what it recorded." + _TAIL,
    "key_changed": "Your relay's transparency log did not check out: your relay's signing key changed without "
                   "a signed hand-over." + _TAIL,
    "stopped": "Your relay's transparency log did not check out: your relay stopped serving the log it kept." + _TAIL,
}
NOTICE = NOTICES["rewritten"]
FIRST_CHECK_SPREAD = 600      # the first check comes 120 s + 0..600 s after start
INTERVAL_JITTER = 0.2         # then every interval +-20%
RETRY_JITTER = 0.1            # a retry after unreachable keeps its delay, +-10%
FIRST_AGAIN = "first look again: the stored head was missing; a change made meanwhile would not show"
FRAME_TYPE = "log_check"


def enabled(env: dict[str, str] | None = None) -> bool:
    """The agent's person's setting: on unless it says off."""
    raw = (os.environ if env is None else env).get(SETTING) or ""
    return raw.strip().lower() not in OFF_VALUES


def record_path(home: str | Path | None) -> Path | None:
    """Where the last result is kept, or None without a Hermes profile (then it is kept in memory only)."""
    return Path(home) / "plugin-data" / "emporia" / RECORD if home else None


def read(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path | None, data: dict[str, Any]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def problem_key(text: str) -> str:
    """Which problem this is, ignoring the numbers in it (a log that shrank is one problem on every day)."""
    return hashlib.sha256(re.sub(r"\d+", "#", text).encode()).hexdigest()[:12]


def summarize(result: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The record for one `verify_relay` result: words and numbers only."""
    status = str(result.get("status") or "")
    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    # An unreachable relay is not a problem; `verify_relay` lists its failure to answer among the problems.
    problems = [] if status in ("unreachable", "off") else [str(p)[:300] for p in result.get("problems") or []]
    if not problems and not result.get("ok") and status not in ("unreachable", "off", ""):
        problems = [str(result.get("detail") or status)[:300]]
    kind = "stopped" if status == "stopped" else ""
    if not kind and any("key you never pinned" in p for p in problems):
        kind = "key_changed"
    return {"status": status, "ok": not problems and status != "unreachable", "kind": kind or "rewritten",
            "size": state.get("size") if isinstance(state.get("size"), int) else None,
            "epoch": state.get("epoch", 1) if state else None,
            "checked_at": now.isoformat(), "problems": problems}


def line(record: dict[str, Any], now: datetime | None = None) -> str:
    """One read-only line for status, doctor and the dashboard."""
    if not record or not record.get("checked_at"):
        return "Log not checked yet (the agent's bridge checks its relay's transparency log once a day)"
    when = _ago(record["checked_at"], now or datetime.now(UTC))
    status, problems = record.get("status"), record.get("problems") or []
    if problems:
        return f"Log checked {when}: PROBLEM: {problems[0]}"
    if status == "off":
        return f"Log checked {when}: off on this relay"
    if status == "unreachable":
        return f"Log checked {when}: the relay did not answer"
    size = record.get("size")
    if record.get("first_again"):
        return f"Log checked {when}: {FIRST_AGAIN}"
    return f"Log checked {when}: OK" + (f", {size} entr{'y' if size == 1 else 'ies'}" if isinstance(size, int) else "")


def _ago(iso: str, now: datetime) -> str:
    try:
        secs = int((now - datetime.fromisoformat(iso)).total_seconds())
    except ValueError:
        return "at an unknown time"
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{round(secs / 60)} min ago"
    if secs < 48 * 3600:
        return f"{round(secs / 3600)} h ago"
    return f"{secs // 86400} days ago"


class Witness:
    """The schedule and the record. `clock` returns seconds (injected in tests); `verify(relay)` is
    `logcheck.verify_relay`."""

    def __init__(self, relay: str, path: Path | None, *, verify: Callable[[str], dict[str, Any]] | None = None,
                 clock: Callable[[], float] = time.monotonic, now: Callable[[], datetime] | None = None,
                 rng: Callable[[], float] = random.random) -> None:
        self.relay = relay.rstrip("/")
        self.path = path
        self._verify = verify
        self._clock = clock
        self._now = now or (lambda: datetime.now(UTC))
        self._rng = rng
        self.next_at = clock() + FIRST_CHECK_SECONDS + self._rng() * FIRST_CHECK_SPREAD
        self._busy = threading.Lock()
        self._record = read(path)

    def due(self) -> bool:
        return self._clock() >= self.next_at and not self._busy.locked()

    def run(self) -> dict[str, Any]:
        """Check once (blocking: call it in a thread). Returns the record, with `notice` true when a problem
        not told before is in it. Never raises."""
        if not self._busy.acquire(blocking=False):
            return {**self._record, "notice": False}
        try:
            verify = self._verify
            if verify is None:
                from emporia import logcheck

                def verify(relay: str) -> dict[str, Any]:
                    return logcheck.verify_relay(relay, compare_with=[])      # the agent's own relay only
            try:
                rec = summarize(verify(self.relay), self._now())
            except Exception as e:  # noqa: BLE001 — a failed check is a quiet one; the next try is soon
                rec = {"status": "unreachable", "ok": False, "size": None, "epoch": None,
                       "checked_at": self._now().isoformat(), "problems": [], "error": type(e).__name__}
            before = self._record
            if rec["status"] == "unreachable":
                # An unreachable relay does not erase an earlier problem: keep what was known, update the time.
                rec = {**before, "status": "unreachable", "checked_at": rec["checked_at"],
                       "problems": before.get("problems") or [], "size": before.get("size"),
                       "epoch": before.get("epoch"), **({"error": rec["error"]} if "error" in rec else {})}
                rec["ok"] = False
            elif rec["status"] == "first" and before.get("status") == "ok" and isinstance(before.get("size"), int):
                rec["first_again"] = True
            noticed = [k for k in before.get("noticed") or [] if isinstance(k, str)]
            if not rec["problems"] and rec["status"] != "unreachable":
                noticed = []          # a clean check: a problem that returns is told again
            new = [k for k in dict.fromkeys(problem_key(p) for p in rec["problems"]) if k not in noticed]
            rec["noticed"] = (noticed + new)[-KEEP_NOTICED:]
            rec["relay"] = self.relay
            self._record = rec
            _write(self.path, rec)
            base, spread = ((RETRY_SECONDS, RETRY_JITTER) if rec["status"] == "unreachable"
                            else (CHECK_EVERY_SECONDS, INTERVAL_JITTER))
            self.next_at = self._clock() + base * (1 + (2 * self._rng() - 1) * spread)
            return {**rec, "notice": bool(new)}
        finally:
            self._busy.release()


def notice_line(name: str, record: dict[str, Any]) -> str:
    """The bridge's stdout line for a problem: fixed text, sent as the relay's own kind (`emporia:relay`)."""
    key = problem_key((record.get("problems") or [""])[0])
    text = NOTICES.get(record.get("kind") or "", NOTICE).format(name=name)
    return json.dumps({"v": 1, "text": text, "sender": "emporia:relay",
                       "frame_type": FRAME_TYPE, "id": f"log-check-{key}"})
