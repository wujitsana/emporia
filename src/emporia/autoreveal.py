"""Opens and reveals that need no thought, done by the agent's bridge (no model turn).

A Tesseract round is sealed (a hash goes to the relay), then, once everyone has sealed, opened: the move and nonce
the agent kept on its own machine go to the relay. The Lore's passage and pick work the same way. The second
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

The tools (`tesseract_open`, `lore_reveal`) stay as the fallback and do the same thing.
"""

from __future__ import annotations

import logging
import os
import re
import contextlib
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
LOOK_BACK_DAYS, LOOK_AT_TABLES, LORE_LOOK_BACK_DAYS = 10, 6, 3
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


def local_lore_waits(agent_id: str, directory: Path, relay_url: str, now: float | None = None) -> bool:
    """Whether a passage or pick of this agent's is kept here from the last few days (the files `lore_write` and
    `lore_vote` keep; the naming of `lore_client.Lore._path`, whose host part is the relay URL as the
    tools see it). A local look, so an agent that plays no Lore never asks the relay about it."""
    now = time.time() if now is None else now
    clean = "".join(c for c in str(agent_id) if c.isalnum() or c in "_-")
    host = "".join(c if c.isalnum() else "_" for c in relay_url)
    try:
        for what in ("passage", "pick"):
            for p in Path(directory).glob(f"{clean}.{host}.lore.{what}.*.json"):
                if now - p.stat().st_mtime < LORE_LOOK_BACK_DAYS * 86_400:
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


#: what the relay says to a step that is already done (modules/tesseract.py, routes/lore.py), each with a 409.
#: Any other refusal, whatever words it has, is a refusal: the agent is woken to see it.
_DONE_DETAILS = ("you already did your open for round ", "you already did your reveal for round ", "you already revealed",
                 "you have already opened your passage", "you have already opened your pick",
                 "this passage is already on the relay")


def _already(res: dict[str, Any]) -> bool:
    """True when the relay answered 409 with one of its own already-done texts."""
    if str(res.get("error") or "").strip() != "Relay 409":
        return False
    detail = res.get("detail")
    if isinstance(detail, dict):
        detail = detail.get("detail") or detail.get("message")
    return isinstance(detail, str) and detail.strip().lower().startswith(_DONE_DETAILS)


class AutoReveal:
    """`table` and `lore` build the agent's own client (`tesseract_client.Table`, `lore_client.Lore`:
    the code the tools run), `local` lists the tables with a sealed push on this machine and whether a Lore value
    waits locally. Everything here is blocking; the bridge runs it in a thread.

    A relay the agent visits is watched the same way: `relays()` lists the visited relays (not the home), `local_for(url)`
    is `local` for one of them, and `scope(url)` is a context manager that points `table` and `lore` at that relay
    for the calls made inside it. A value sealed on a relay is opened on that relay and no other."""

    def __init__(self, agent_id: str, *, table: Callable[[], Any], lore: Callable[[], Any],
                 local: Callable[[], tuple[list[str], bool]], is_on: Callable[[], bool] = enabled,
                 sleep: Callable[[float], None] = time.sleep, log: Callable[..., None] = logger.info,
                 relays: Callable[[], list[str]] | None = None,
                 local_for: Callable[[str], tuple[list[str], bool]] | None = None,
                 scope: Callable[[str], Any] | None = None):
        self.agent_id = agent_id
        self._table, self._lore, self._local = table, lore, local
        self._relays, self._local_for, self._scope = relays, local_for, scope
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
    def _in(self, relay: str):
        """The calls made inside it go to `relay` (a visited relay); the home relay when there is none."""
        return self._scope(relay) if relay and self._scope else contextlib.nullcontext()

    def tesseract(self, session_id: str, relay: str = "") -> str:
        """Open what is due at one table, when the agent sealed it here. 'done' (or already done), 'skipped' (nothing
        due, or not safe: the model wakes as usual) or 'failed' (the relay refused: the model wakes as usual).
        `relay`: the visited relay the table is on (empty: the home relay)."""
        if not self._on() or not _ID.fullmatch(session_id or ""):
            return "skipped"
        with self._in(relay):
            return self._tesseract(session_id, f"{relay}#{session_id}" if relay else session_id)

    def _tesseract(self, session_id: str, tag: str) -> str:
        with self._lock(tag):
            try:
                table = self._table()
                chk = table.auto_check(session_id)
                kind, epoch = chk.get("kind"), chk.get("epoch")
                if chk.get("over"):
                    self._over.add(tag)
                key = (tag, kind or "", epoch if epoch is not None else "")
                if not chk.get("go"):
                    # a redelivered frame for a step this bridge already did is covered, not a reason to wake
                    return "done" if kind and key in self._done else "skipped"
                if key in self._done:
                    return "done"
                for attempt in range(RATE_RETRIES + 1):
                    res = table.open(session_id, by="bridge")
                    if res.get("success") and (res.get("nothing_due") or res.get("done")):
                        # nothing was sent: not "done for you", so the model wakes as usual
                        self._log("auto-%s: table %s had nothing due; the agent is woken as usual", chk["kind"],
                                  session_id)
                        return "skipped"
                    ok = bool(res.get("success")) and bool(res.get("opened") or res.get("revealed"))
                    if not ok and _already(res):
                        ok = True
                    if ok:
                        what = (f"your open for round {epoch} was done for you" if chk["kind"] == "open"
                                else "your secret reveal was done for you")
                        self._remember(tag, key, what)
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

    # ── Lore ─────────────────────────────────────────────────────────────────────────────────
    def lore(self, relay: str = "") -> str:
        """Open the agent's passage (Open window) or pick (Count window) when it sealed it here. `relay`: the visited
        relay it was sealed on (empty: the home relay)."""
        if not self._on():
            return "skipped"
        with self._in(relay):
            return self._lore_open(f"lore#{relay}" if relay else "lore")

    def _lore_open(self, tag: str) -> str:
        with self._lock(tag):
            try:
                book = self._lore()
                chk = book.auto_check()
                if not chk.get("go"):
                    return "skipped"
                key = (tag, chk["kind"], chk["chapter"])
                if key in self._done:
                    return "done"
                for attempt in range(RATE_RETRIES + 1):
                    res = book.reveal(chapter=chk["chapter"], kind=chk["kind"])
                    ok = bool(res.get("success")) and bool(res.get("opened"))     # a note of "nothing due" sent nothing
                    if not ok and _already(res):
                        ok = True
                    if ok:
                        self._remember(tag, key, f"your {chk['kind']} of the chapter was opened for you")
                        self._log("auto-reveal: lore %s of %s sent", chk["kind"], chk["chapter"])
                        return "done"
                    wait = _retry_in(res)
                    if wait is not None and attempt < RATE_RETRIES:
                        self._log("auto-reveal: lore rate limited, waiting %.0f s", wait)
                        self._sleep(wait)
                        continue
                    self._log("auto-reveal: lore %s refused (%s)", chk["kind"], str(res.get("error"))[:120])
                    return "failed"
                return "failed"
            except Exception as e:  # noqa: BLE001
                self._log("auto-reveal: lore could not be checked (%s)", type(e).__name__)
                return "failed"

    # ── the bridge's two entry points ─────────────────────────────────────────────────────────────
    def frame(self, frame: dict[str, Any], relay: str = "") -> bool:
        """True when the relay's frame needs no model turn because the step it names was done here. Only a `your_turn`
        for a Tesseract table is looked at, and only its session id is used: the relay's state decides. `relay`: the
        visited relay the frame came from (empty: the home relay)."""
        if not isinstance(frame, dict) or frame.get("type") != "your_turn" or frame.get("module_type") != TESSERACT:
            return False
        return self.tesseract(str(frame.get("session_id") or ""), relay) == "done"

    def look(self) -> None:
        """The periodic pass, also the first one after a start: finish what is sealed here and due now (a restart in
        the middle of a round, a missed frame). It reads local files first and asks the relay only about those."""
        if not self._on():
            return
        sessions, lore_waits = self._local()
        for sid in sessions:
            if sid not in self._over:
                self.tesseract(sid)
        if lore_waits:
            self.lore()
        if self._relays is None or self._local_for is None:
            return
        try:
            visited = list(self._relays())
        except Exception:  # noqa: BLE001 — no list of visits is no visited relays; the home was looked at above
            return
        for url in visited:
            try:
                sessions, lore_waits = self._local_for(url)
            except Exception:  # noqa: BLE001 — one relay's files never stop the next
                continue
            for sid in sessions:
                if f"{url}#{sid}" not in self._over:
                    self.tesseract(sid, url)
            if lore_waits:
                self.lore(url)
