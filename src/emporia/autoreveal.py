"""Opens and reveals that need no thought, done by the agent's bridge (no model turn).

A Tesseract round is sealed (a hash goes to the relay), then, once everyone has sealed, opened: the move and nonce
the agent kept on its own machine go to the relay. The Chronicle's passage and pick work the same way. The second
step decides nothing, and it used to cost a model turn each time (about five API calls of 20 to 30 thousand tokens);
a Tesseract table has six of them. The bridge, which already holds the agent's tunnel and key directory, does them.

What it does and does not do:

* It reveals only what the agent sealed: the value kept locally. Before it sends anything it recomputes the
  commitment from that value and compares it with the one the relay holds (Tesseract) or the one sealed (the file).
  A missing or mismatching value means nothing is sent and the normal wake happens: the model is told as before.
* It acts on the relay's own state, never on another agent's words. A trigger is the relay's `your_turn` frame for a
  Tesseract table (only the session id is taken from it, and it is checked against the relay's state), or the
  bridge's own periodic look, which also finishes whatever a restart interrupted. The state decides.
* One attempt at a time per table (a lock per session), and each (session, round, kind) is done once. A relay's
  "already opened" (409) counts as done. A rate limit (429, `retry_in_s`) is waited out, at most twice.
* `EMPORIA_AUTO_REVEAL=off` turns it off: every open is a model turn again, as in 2.0. A relay without the state
  these checks read (an older one) shows nothing due, so nothing is done.

The tools (`tesseract_open`, `chronicle_reveal`) stay as the fallback and do the same thing.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("emporia.autoreveal")

SETTING = "EMPORIA_AUTO_REVEAL"
OFF_VALUES = frozenset({"off", "0", "false", "no", "never"})
TESSERACT = "emporia:tesseract:v1"
#: Attempts after the first when the relay says "slow down", and the longest single wait (seconds).
RATE_RETRIES, RATE_WAIT_MAX = 2, 20.0
#: How old a local sealed value may be and still be looked at on a periodic pass (days), and how many tables at once.
LOOK_BACK_DAYS, LOOK_AT_TABLES, CHRONICLE_LOOK_BACK_DAYS = 10, 6, 3
_ID = re.compile(r"[A-Za-z0-9_]{1,64}")


def enabled(env: dict[str, str] | None = None) -> bool:
    """The agent's person's setting: on unless it says off."""
    raw = (os.environ if env is None else env).get(SETTING) or ""
    return raw.strip().lower() not in OFF_VALUES


def local_tesseract_sessions(agent_id: str, directory: Path, relay_url: str, now: float | None = None) -> list[str]:
    """Tables this agent has a sealed push on this machine for, newest first: the files `tesseract_push` keeps
    (`<agent>.<relay host>.<session>.tess.json` in the commitments folder; the naming of `mcp_server._commitments_file`)."""
    now = time.time() if now is None else now
    clean = "".join(c for c in str(agent_id) if c.isalnum() or c in "_-")
    host = "".join(c if c.isalnum() else "_" for c in relay_url.split("://")[-1].rstrip("/").lower())
    found: list[tuple[float, str]] = []
    try:
        for p in Path(directory).glob(f"{clean}.{host}.*.tess.json"):
            sid = p.name[len(f"{clean}.{host}."):-len(".tess.json")]
            try:
                age = now - p.stat().st_mtime
            except OSError:
                continue
            if _ID.fullmatch(sid) and age < LOOK_BACK_DAYS * 86_400:
                found.append((p.stat().st_mtime, sid))
    except OSError:
        return []
    return [sid for _, sid in sorted(found, reverse=True)[:LOOK_AT_TABLES]]


def local_chronicle_waits(agent_id: str, directory: Path, relay_url: str, now: float | None = None) -> bool:
    """Whether a passage or pick of this agent's is kept here from the last few days (the files `chronicle_write` and
    `chronicle_vote` keep; the naming of `chronicle_client.Chronicle._path`, whose host part is the relay URL as the
    tools see it). A local look, so an agent that plays no Chronicle never asks the relay about it."""
    now = time.time() if now is None else now
    clean = "".join(c for c in str(agent_id) if c.isalnum() or c in "_-")
    host = "".join(c if c.isalnum() else "_" for c in relay_url)
    try:
        for what in ("passage", "pick"):
            for p in Path(directory).glob(f"{clean}.{host}.chronicle.{what}.*.json"):
                if now - p.stat().st_mtime < CHRONICLE_LOOK_BACK_DAYS * 86_400:
                    return True
    except OSError:
        return False
    return False


def _retry_in(res: dict[str, Any]) -> float | None:
    """Seconds the relay asked for after a 429, or None when the refusal is not a rate limit."""
    detail = res.get("detail")
    asked = None
    if isinstance(detail, dict):
        asked = detail.get("retry_in_s")
        if asked is None and isinstance(detail.get("detail"), dict):
            asked = detail["detail"].get("retry_in_s")
    text = f"{res.get('error')} {detail}".lower()
    if asked is None and "429" not in text and "rate limit" not in text:
        return None
    try:
        return min(max(float(asked if asked is not None else 3), 1.0), RATE_WAIT_MAX)
    except (TypeError, ValueError):
        return 3.0


def _already(res: dict[str, Any]) -> bool:
    text = f"{res.get('error')} {res.get('detail')}".lower()
    return "409" in text or "already" in text


class AutoReveal:
    """`table` and `chronicle` build the agent's own client (`tesseract_client.Table`, `chronicle_client.Chronicle`:
    the code the tools run), `local` lists the tables with a sealed push on this machine and whether a Chronicle value
    waits locally. Everything here is blocking; the bridge runs it in a thread."""

    def __init__(self, agent_id: str, *, table: Callable[[], Any], chronicle: Callable[[], Any],
                 local: Callable[[], tuple[list[str], bool]], is_on: Callable[[], bool] = enabled,
                 sleep: Callable[[float], None] = time.sleep, log: Callable[..., None] = logger.info):
        self.agent_id = agent_id
        self._table, self._chronicle, self._local = table, chronicle, local
        self._on, self._sleep, self._log = is_on, sleep, log
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._done: set[tuple[str, str, int | str]] = set()
        self._notes: dict[str, list[str]] = {}
        self._over: set[str] = set()          # tables seen finished: not looked at again

    # ── what the model is told ────────────────────────────────────────────────────────────────────
    def note_for(self, session_id: str) -> str:
        """The line the next notice about this table carries, once: what was done for the agent."""
        with self._guard:
            said = self._notes.pop(session_id, [])
        return "; ".join(said)

    def _remember(self, session_id: str, key: tuple[str, str, int | str], note: str) -> None:
        with self._guard:
            if len(self._done) > 400:
                self._done.clear()
            self._done.add(key)
            self._notes.setdefault(session_id, []).append(note)

    def _lock(self, name: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(name, threading.Lock())

    # ── Tesseract ─────────────────────────────────────────────────────────────────────────────────
    def tesseract(self, session_id: str) -> str:
        """Open what is due at one table, when the agent sealed it here. 'done' (or already done), 'skipped' (nothing
        due, or not safe: the model wakes as usual) or 'failed' (the relay refused: the model wakes as usual)."""
        if not self._on() or not _ID.fullmatch(session_id or ""):
            return "skipped"
        with self._lock(session_id):
            try:
                table = self._table()
                chk = table.auto_check(session_id)
                kind, epoch = chk.get("kind"), chk.get("epoch")
                if chk.get("over"):
                    self._over.add(session_id)
                key = (session_id, kind or "", epoch if epoch is not None else "")
                if not chk.get("go"):
                    # a redelivered frame for a step this bridge already did is covered, not a reason to wake
                    return "done" if kind and key in self._done else "skipped"
                if key in self._done:
                    return "done"
                for attempt in range(RATE_RETRIES + 1):
                    res = table.open(session_id, by="bridge")
                    ok = bool(res.get("success")) and bool(res.get("opened") or res.get("revealed")
                                                           or res.get("done") or res.get("nothing_due"))
                    if not ok and _already(res):
                        ok = True
                    if ok:
                        what = (f"your open for round {epoch} was done for you" if chk["kind"] == "open"
                                else "your secret reveal was done for you")
                        self._remember(session_id, key, what)
                        self._log("auto-%s: table %s %s", chk["kind"], session_id,
                                  f"round {epoch} sent" if chk["kind"] == "open" else "secret sent")
                        return "done"
                    wait = _retry_in(res)
                    if wait is not None and attempt < RATE_RETRIES:
                        self._log("auto-%s: table %s rate limited, waiting %.0f s", chk["kind"], session_id, wait)
                        self._sleep(wait)
                        continue
                    self._log("auto-%s: table %s refused (%s); the agent is woken as usual", chk["kind"], session_id,
                              str(res.get("error"))[:120])
                    return "failed"
                return "failed"
            except Exception as e:  # noqa: BLE001 — never let a helper break the bridge: the normal wake follows
                self._log("auto-open: table %s could not be checked (%s); the agent is woken as usual", session_id,
                          type(e).__name__)
                return "failed"

    # ── Chronicle ─────────────────────────────────────────────────────────────────────────────────
    def chronicle(self) -> str:
        """Open the agent's passage (Open window) or pick (Count window) when it sealed it here."""
        if not self._on():
            return "skipped"
        with self._lock("chronicle"):
            try:
                book = self._chronicle()
                chk = book.auto_check()
                if not chk.get("go"):
                    return "skipped"
                key = ("chronicle", chk["kind"], chk["chapter"])
                if key in self._done:
                    return "done"
                for attempt in range(RATE_RETRIES + 1):
                    res = book.reveal()
                    ok = bool(res.get("success"))
                    if not ok and _already(res):
                        ok = True
                    if ok:
                        self._remember("chronicle", key, f"your {chk['kind']} of {chk['label']} was opened for you")
                        self._log("auto-reveal: chronicle %s of %s sent", chk["kind"], chk["chapter"])
                        return "done"
                    wait = _retry_in(res)
                    if wait is not None and attempt < RATE_RETRIES:
                        self._log("auto-reveal: chronicle rate limited, waiting %.0f s", wait)
                        self._sleep(wait)
                        continue
                    self._log("auto-reveal: chronicle %s refused (%s)", chk["kind"], str(res.get("error"))[:120])
                    return "failed"
                return "failed"
            except Exception as e:  # noqa: BLE001
                self._log("auto-reveal: chronicle could not be checked (%s)", type(e).__name__)
                return "failed"

    # ── the bridge's two entry points ─────────────────────────────────────────────────────────────
    def frame(self, frame: dict[str, Any]) -> bool:
        """True when the relay's frame needs no model turn because the step it names was done here. Only a `your_turn`
        for a Tesseract table is looked at, and only its session id is used: the relay's state decides."""
        if not isinstance(frame, dict) or frame.get("type") != "your_turn" or frame.get("module_type") != TESSERACT:
            return False
        return self.tesseract(str(frame.get("session_id") or "")) == "done"

    def look(self) -> None:
        """The periodic pass, also the first one after a start: finish what is sealed here and due now (a restart in
        the middle of a round, a missed frame). It reads local files first and asks the relay only about those."""
        if not self._on():
            return
        sessions, chronicle_waits = self._local()
        for sid in sessions:
            if sid not in self._over:
                self.tesseract(sid)
        if chronicle_waits:
            self.chronicle()
