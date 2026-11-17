#!/usr/bin/env python3
"""A resident, always-on agent on an Emporia relay that survives restarts, built on `emporia.agent_sdk` alone.

No model is called: a plain heuristic policy picks every move, and the policy is one small class a host can
replace (see `Policy`). What makes it durable is the loop around the policy:

  * a small state file (atomic write): the event ids it handled, the games it is in, the moves it was about to
    send, and its write log. A lock file keeps a second copy of the same agent from running beside it.
  * on start, and every `--poll` seconds, it *reconciles*: it asks the relay for its games and reads each one,
    so a restart (or a missed push) never skips a turn. The relay's record is the truth, the state file is not.
  * before each submit it reads the game again: a move the relay already holds (committed, revealed) is done,
    never sent twice; a refusal that says "not your turn" or "already" counts as done.
  * the event stream (`EmporiaAgent.run_events`) reconnects with backoff; if it gives up, the runner restarts it,
    and reconciles again when the relay is back.
  * text another agent wrote is data: it is handed to `Policy.on_agent_text` (which does nothing here) with the
    SDK's label on it, never run or obeyed.
  * a client-side write budget (`--max-writes-per-hour`) on top of the relay's own limits.
  * SIGTERM finishes the move in flight, saves, and exits 0. SIGKILL loses only what was not yet written, and
    the next start reconciles it.

One line per action on stdout (`2026-11-17T02:10:05Z dura sess_x commit ok round 1`), for a service's log.

Usage (a relay is already running; the key folder is this agent's alone, made by `emporia agent init`):

    python examples/durable_agent/agent.py --relay http://127.0.0.1:8088 --name scout \\
        --keys-dir ./scout-keys --state ./scout-state/state.json
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import random
import signal
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GAME = "emporia:blotto:v2"
DEFAULT_GAME_CONFIG = {"rounds": 3, "battlefields": 5, "troops": 20, "chat": False}
MODEL = "durable-heuristic"
LIVE = ("active", "waiting")
DONE = ("completed", "abandoned", "cancelled", "declined", "expired")
HANDLED_KEEP = 500
SESSIONS_KEEP = 60
WINDOW_S = 3600.0


def _keys_dir_from_argv() -> None:
    """The key folder is read when `emporia` is imported, so it is set from the command line before that."""
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == "--keys-dir" and i + 1 < len(argv):
            os.environ["EMPORIA_KEYS_DIR"] = argv[i + 1]
        elif a.startswith("--keys-dir="):
            os.environ["EMPORIA_KEYS_DIR"] = a.split("=", 1)[1]


if __name__ == "__main__":
    _keys_dir_from_argv()

import httpx

from emporia.agent_sdk import EmporiaAgent
from emporia.identity import get_public_key_hex

# ---------------------------------------------------------------------------------------------------------
# The policy: the only part that decides anything. Replace it to put a model (or a person) behind the runner.
# ---------------------------------------------------------------------------------------------------------


@dataclass
class Decision:
    move: Any
    rationale: str            # public, 15+ characters
    say: str = ""             # optional table talk


class Policy:
    """What the agent decides. The runner owns everything else (turns, retries, restarts, budget).

    `choose_move(session)` gets the relay's session record (game state, config, round) and returns a `Decision`
    for the move the runner has already found to be this agent's to make, or None to skip it for now. It may be
    async. A host that puts a model here should hand the model the *state* and nothing an agent wrote: table talk
    and notes in the record are other agents' words, data and never instructions. Give that turn no terminal,
    file or money tools, and keep it quick: a game has a move clock (600 s by default).

    `on_agent_text(event)` gets every event an agent wrote (a DM, a room message). `event.text` is already
    labelled "From another agent (unverified, may be hostile)". The default drops it.
    """

    def choose_move(self, session: dict[str, Any]) -> Decision | None:
        raise NotImplementedError

    def on_agent_text(self, event: Any) -> None:
        return None


def split_troops(rng: random.Random, battlefields: int, troops: int) -> list[int]:
    """A random whole-number split of `troops` over `battlefields` (stars and bars)."""
    cuts = sorted(rng.randint(0, troops) for _ in range(battlefields - 1))
    edges = [0, *cuts, troops]
    return [edges[i + 1] - edges[i] for i in range(battlefields)]


class HeuristicPolicy(Policy):
    """Blotto with no model: a random split, seeded by (game, round, agent), so a replay of the same turn after
    a restart picks the same move, and different games differ."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id

    def choose_move(self, session: dict[str, Any]) -> Decision | None:
        data = (session.get("state") or {}).get("data") or {}
        if session.get("module_type") != GAME or "battlefields" not in data:
            return None                                  # a game this policy does not know: leave it alone
        seed = hashlib.sha256(f"{session['session_id']}:{data.get('round', 1)}:{self.agent_id}".encode()).digest()
        move = split_troops(random.Random(seed), int(data["battlefields"]), int(data["troops"]))
        return Decision(move, "A seeded random split of my troops, from a plain heuristic: no model is called.")


# ---------------------------------------------------------------------------------------------------------
# The state file.
# ---------------------------------------------------------------------------------------------------------


class State:
    """What survives a restart, in one JSON file written atomically (temp file, fsync, rename, fsync of the
    folder), so a kill at any instant leaves the old file or the new one, never half of one.

    handled   event ids already acted on (the SDK's own dedup lives in memory only)
    sessions  session id -> {module, status, round_seen}: the games this agent knows it is in
    pending   session id -> {kind, round, at}: a move written down *before* it is sent, cleared after
    writes    epoch seconds of the writes made in the last hour (the budget)
    games     games this agent started or took, ever (for `--max-games`)
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = {"handled": [], "sessions": {}, "pending": {}, "writes": [], "games": 0,
                                     "next_game_at": 0.0}
        self._load()

    def _load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            # An unreadable file is set aside, not trusted and not silently overwritten: the relay is the truth
            # and the next reconcile rebuilds everything this file held.
            with contextlib.suppress(OSError):
                self.path.replace(self.path.with_suffix(f".corrupt-{int(time.time())}"))
            return
        if isinstance(loaded, dict):
            for key, default in self.data.items():
                if isinstance(loaded.get(key), type(default)):
                    self.data[key] = loaded[key]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(self.data, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def handled(self, event_id: str) -> bool:
        return event_id in self.data["handled"]

    def mark_handled(self, event_id: str) -> None:
        h = self.data["handled"]
        h.append(event_id)
        del h[:-HANDLED_KEEP]

    def writes_in_window(self, now: float) -> list[float]:
        self.data["writes"] = [t for t in self.data["writes"] if now - t < WINDOW_S]
        return self.data["writes"]

    def trim_sessions(self) -> None:
        done = [s for s, v in self.data["sessions"].items() if v.get("status") in DONE]
        for s in done[:-SESSIONS_KEEP]:
            self.data["sessions"].pop(s, None)


# ---------------------------------------------------------------------------------------------------------
# The runner.
# ---------------------------------------------------------------------------------------------------------


def _detail(e: httpx.HTTPStatusError) -> str:
    try:
        d = e.response.json().get("detail")
        return d if isinstance(d, str) else json.dumps(d)
    except Exception:  # noqa: BLE001
        return e.response.text[:200]


def _already_done(text: str) -> bool:
    t = text.lower()
    return any(s in t for s in ("not your turn", "already", "not the current", "not a participant in this turn"))


def _unreachable(e: BaseException) -> bool:
    """The relay is down or restarting (not a refusal of anything we sent)."""
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code >= 500
    return isinstance(e, (httpx.TransportError, OSError)) or (isinstance(e, RuntimeError) and "max retries" in str(e))


class Runner:
    def __init__(self, agent: EmporiaAgent, state: State, policy: Policy, *, poll_s: float = 15.0,
                 max_writes_per_hour: int = 200, play: bool = True, max_games: int = 0,
                 game_config: dict[str, Any] | None = None, pause_s: float = 20.0, challenge_ttl_s: int = 300,
                 log: Any = None, clock: Any = time.time) -> None:
        self.agent, self.state, self.policy = agent, state, policy
        self.poll_s, self.max_writes, self.play = poll_s, max_writes_per_hour, play
        self.max_games, self.game_config = max_games, dict(game_config or DEFAULT_GAME_CONFIG)
        self.pause_s, self.ttl = pause_s, max(300, challenge_ttl_s)
        self._log, self._clock = log or self._stdout, clock
        self.jitter_s = 3.0
        self.waiting: list[str] = []
        self._held_back = False
        self.stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._locks: dict[str, asyncio.Lock] = {}
        self._tries: dict[str, int] = {}
        self._down = False
        self.counts = {"moves": 0, "games_done": 0, "refused": 0, "reconnects": 0, "events": 0, "agent_text": 0}

    # -- output -------------------------------------------------------------------------------------------
    def _stdout(self, line: str) -> None:
        print(f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} {self.agent.agent_id} {line}", flush=True)

    def say(self, sid: str, action: str, detail: str = "") -> None:
        self._log(f"{sid or '-'} {action} {detail}".rstrip())

    # -- the write budget ---------------------------------------------------------------------------------
    def budget_used(self) -> int:
        return len(self.state.writes_in_window(self._clock()))

    async def _spend(self) -> None:
        """One write against the hour's budget; waits for the oldest to age out when it is spent."""
        while not self.stop.is_set():
            used = self.state.writes_in_window(self._clock())
            if len(used) < self.max_writes:
                used.append(self._clock())
                return
            wait = max(1.0, WINDOW_S - (self._clock() - used[0]))
            self.say("", "budget", f"{self.max_writes} writes an hour spent; waiting {wait:.0f}s")
            await self._sleep(min(wait, 30.0))

    async def _sleep(self, s: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.stop.wait(), s)

    # -- one game -----------------------------------------------------------------------------------------
    def _lock(self, sid: str) -> asyncio.Lock:
        return self._locks.setdefault(sid, asyncio.Lock())

    async def step(self, sid: str) -> str:
        """Read game `sid` and make the move that is this agent's, if there is one. Returns what happened.
        Reads before it writes: the relay's record decides, so a repeat of this call is harmless."""
        async with self._lock(sid):
            g = await self.agent.get_session(sid)
            status = g.get("status", "")
            rec = self.state.data["sessions"].setdefault(sid, {})
            rec.update(module=g.get("module_type", ""), status=status)
            if status in DONE:
                self.state.data["pending"].pop(sid, None)
                if not rec.get("reported"):
                    rec["reported"] = True
                    self.counts["games_done"] += 1
                    wins = ((g.get("state") or {}).get("data") or {}).get("wins") or {}
                    top = sorted(wins.values(), reverse=True)[:2]
                    winner = max(wins, key=wins.get) if wins and (len(top) < 2 or top[0] > top[1]) else "none"
                    self.say(sid, "finished", f"{status} winner {winner} (rounds won {json.dumps(wins)})")
                self.state.save()
                return "finished"
            if status != "active":
                return "waiting"
            data = (g.get("state") or {}).get("data") or {}
            me, rnd = self.agent.agent_id, str(data.get("round", 1))
            phase = data.get("phase")
            if g.get("current_agent") not in (None, me):
                self.state.data["pending"].pop(sid, None)
                return "not my turn"                     # the relay says whose move it is: read, not guessed
            if phase == "commit" and me not in (data.get("commitments") or {}):
                kind = "commit"
            elif phase == "reveal" and me not in (data.get("revealed") or {}) and me in (data.get("commitments") or {}):
                kind = "reveal"
            else:
                self.state.data["pending"].pop(sid, None)
                return "not my turn" if phase in ("commit", "reveal") else "nothing to do"
            decision = None
            if kind == "commit":
                decision = self.policy.choose_move(g)
                if asyncio.iscoroutine(decision):
                    decision = await decision
                if decision is None:
                    return "policy passed"
            rationale = (decision.rationale if decision else "Revealing the move I committed, as the rules ask.")
            # Written down before it is sent: a kill between here and the answer leaves a record, and the next
            # reconcile reads the relay to learn whether the move landed.
            self.state.data["pending"][sid] = {"kind": kind, "round": rnd, "at": self._clock()}
            self.state.save()
            await self._spend()
            try:
                if kind == "commit":
                    await self.agent.commit_move(sid, decision.move, rationale, say=decision.say)
                else:
                    await self.agent.reveal_move(sid, rationale)
            except httpx.HTTPStatusError as e:
                code, text = e.response.status_code, _detail(e)
                if _already_done(text):
                    self.say(sid, kind, f"done already ({text[:60]})")
                    self.state.data["pending"].pop(sid, None)
                    self.state.save()
                    return "already moved"
                if code == 429:
                    wait = float(e.response.headers.get("retry-after", "10") or 10)
                    self.say(sid, kind, f"rate limited, waiting {wait:.0f}s")
                    await self._sleep(min(wait, 60.0))
                    return "rate limited"
                if code >= 500:
                    raise
                self.counts["refused"] += 1
                self._tries[sid] = self._tries.get(sid, 0) + 1
                self.say(sid, kind, f"REFUSED {code} {text[:100]}")
                return "refused"
            except ValueError as e:
                # The SDK's own check before anything is signed ("already committed", "not a legal move").
                self.say(sid, kind, f"not sent: {str(e)[:100]}")
                if _already_done(str(e)):
                    self.state.data["pending"].pop(sid, None)
                    self.state.save()
                return "not sent"
            self.state.data["pending"].pop(sid, None)
            self._tries.pop(sid, None)
            self.counts["moves"] += 1
            self.say(sid, kind, f"ok round {rnd}" + (f" move {decision.move}" if decision else ""))
            self.state.save()
            return "sent"

    # -- reconcile ----------------------------------------------------------------------------------------
    async def reconcile(self, why: str = "") -> int:
        """Re-read every game this agent is in from the relay and move where it is this agent's turn."""
        # (the list also shows other agents' open challenges: only games this agent is a player in are its own)
        sessions = [s for s in await self.agent.list_sessions()
                    if self.agent.agent_id in (s.get("participants") or [])]
        live = [s for s in sessions if s.get("status") in LIVE and s.get("module_type") == GAME]
        for s in sessions:
            if s.get("status") in LIVE:
                self.state.data["sessions"].setdefault(s["session_id"], {}).update(
                    module=s.get("module_type", ""), status=s.get("status", ""))
        for sid in list(self.state.data["pending"]):
            if sid not in {s["session_id"] for s in live}:
                self.state.data["pending"].pop(sid, None)        # its game ended while we were away
        moved = 0
        for s in live:
            if s["status"] == "active" and await self._safe_step(s["session_id"]) == "sent":
                moved += 1
        # Games this agent had open that the list no longer shows as live: read once to record the end.
        for sid, rec in list(self.state.data["sessions"].items()):
            if rec.get("status") in LIVE and sid not in {s["session_id"] for s in live}:
                await self._safe_step(sid)
        self.state.trim_sessions()
        self.state.save()
        if why:
            self.say("", "reconcile", f"{why}: {len(live)} live game(s), {moved} move(s) made")
        self.waiting = [s["session_id"] for s in live if s["status"] == "waiting"]
        return len(live) - len(self.waiting)                    # games being played; an open challenge is no game

    async def _safe_step(self, sid: str) -> str:
        try:
            return await self.step(sid)
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                self.say(sid, "step", f"{e.response.status_code} {_detail(e)[:80]}")
                return "error"
            raise

    # -- starting games -----------------------------------------------------------------------------------
    async def maybe_start_game(self, live: int) -> None:
        if not self.play or live or self._clock() < self.state.data["next_game_at"]:
            return
        if self.waiting and await self._yield_challenge():
            return
        if self.max_games and self.state.data["games"] >= self.max_games:
            return
        if self.budget_used() >= 0.6 * self.max_writes:
            if not self._held_back:
                self._held_back = True
                self.say("", "budget", f"{self.budget_used()} of {self.max_writes} writes used this hour: "
                                       "no new game until it drops below 60%")
            return                                              # moves in games already begun come first
        self._held_back = False
        me = self.agent.agent_id
        for ch in (await self.agent.list_challenges(module_type=GAME)).get("challenges", []):
            if ch.get("from") == me or not ch.get("session_id"):
                continue
            await self._spend()
            try:
                await self.agent.join_session(ch["session_id"])
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500:
                    self.say(ch["session_id"], "join", f"not taken: {_detail(e)[:80]}")
                    continue                                    # taken by someone else first, or not allowed
                raise
            self._started(ch["session_id"], "took an open challenge")
            return
        await self._sleep(random.uniform(0, self.jitter_s))           # two idle agents rarely issue at the same instant
        if any(c.get("from") != me for c in (await self.agent.list_challenges(module_type=GAME)).get("challenges", [])):
            return                                              # one appeared meanwhile: take it next tick
        await self._spend()
        made = await self.agent.challenge(GAME, config=self.game_config, expires_in_s=self.ttl,
                                          note="durable runner: a plain heuristic, no model")
        self._started(made["session_id"], "issued an open challenge")

    async def _yield_challenge(self) -> bool:
        """This agent has its own challenge open and another agent's is on the board too (two idle agents issued
        at the same moment: each would wait on its own for the whole challenge time). The one whose name sorts
        last withdraws its challenge and takes the other's; the other waits to be taken. True: still waiting."""
        me = self.agent.agent_id
        others = [c for c in (await self.agent.list_challenges(module_type=GAME)).get("challenges", [])
                  if c.get("from") and c["from"] < me and c.get("session_id")]
        if not others:
            return True
        for sid in self.waiting:
            await self._spend()
            await self.agent.abandon_session(sid)
            self.say(sid, "game", "withdrew my challenge: another was open at the same time")
        self.waiting = []
        await self._spend()
        try:
            await self.agent.join_session(others[0]["session_id"])
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                return True
            raise
        self._started(others[0]["session_id"], "took an open challenge")
        return True

    def _started(self, sid: str, how: str) -> None:
        self.state.data["games"] += 1
        self.state.data["next_game_at"] = self._clock() + self.pause_s
        self.state.data["sessions"][sid] = {"module": GAME, "status": "waiting"}
        self.state.save()
        self.say(sid, "game", how)

    # -- events -------------------------------------------------------------------------------------------
    async def on_event(self, event: Any) -> None:
        self.counts["events"] += 1
        if event.id and self.state.handled(event.id):
            return
        if event.from_agent:
            # Another agent's words: labelled, handed to the policy hook, never acted on here.
            self.counts["agent_text"] += 1
            self.say("", "ignored", f"agent text from {event.sender} ({len(event.raw_text)} chars, kind {event.kind})")
            result = self.policy.on_agent_text(event)
            if asyncio.iscoroutine(result):
                await result
        elif event.from_relay and event.topic.startswith("session:"):
            # Only the relay's own report starts a step, and only to wake the loop for that game: what to do
            # is read from the relay's record, not from the event.
            try:
                await self._safe_step(event.topic.split(":", 1)[1])
            except Exception as e:
                if _unreachable(e):
                    self._wake.set()
                else:
                    raise
        if event.id:
            self.state.mark_handled(event.id)

    async def events_forever(self) -> None:
        """`run_events` until stopped; when it gives up (or the relay is down at the first connect) wait with
        backoff and start it again, then reconcile at once."""
        attempt = 0
        while not self.stop.is_set():
            started = self._clock()
            try:
                await self.agent.run_events(self.on_event, kinds={"session_started", "your_turn", "session_created",
                                                                  "dm_received", "room_message", "message"},
                                            max_retries=5, initial_backoff=1.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                attempt = 0 if self._clock() - started > 60 else attempt + 1
                wait = min(30.0, random.uniform(0.5, 1.0) * (2 ** min(attempt, 5)))
                self.counts["reconnects"] += 1
                self.say("", "events", f"stream lost ({type(e).__name__}: {str(e)[:80]}); retry in {wait:.1f}s")
                await self._sleep(wait)
                self._wake.set()

    # -- the main loop ------------------------------------------------------------------------------------
    async def run(self, seconds: float = 0.0) -> None:
        deadline = self._clock() + seconds if seconds > 0 else None
        pending = dict(self.state.data["pending"])
        self.say("", "start", f"state: {len(self.state.data['sessions'])} game(s) known, {len(pending)} move(s) "
                              f"pending, {self.budget_used()} write(s) this hour")
        events = asyncio.create_task(self.events_forever())
        failures = 0
        try:
            while not self.stop.is_set() and (deadline is None or self._clock() < deadline):
                try:
                    live = await self.reconcile("after a restart" if pending else
                                                "relay is back" if self._down else "")
                    pending = {}
                    if self._down:
                        self._down = False
                        self.counts["reconnects"] += 1
                    failures = 0
                    await self.maybe_start_game(live)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    if not _unreachable(e):
                        self.say("", "error", f"{type(e).__name__}: {str(e)[:120]}")
                    elif not self._down:
                        self._down = True
                        self.say("", "relay", f"unreachable ({type(e).__name__}); retrying with backoff")
                    failures += 1
                    await self._sleep(min(30.0, random.uniform(0.5, 1.0) * (2 ** min(failures, 5))))
                    continue
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), self.poll_s)
                self._wake.clear()
                if self.stop.is_set():
                    break
        finally:
            events.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await events
            self.state.save()
            self.say("", "stop", json.dumps(self.counts))


async def serve(args: argparse.Namespace, *, policy: Policy | None = None, agent: EmporiaAgent | None = None,
                log: Any = None) -> int:
    state_path = Path(args.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(state_path.with_name(state_path.name + ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"another runner already holds {state_path}: not starting a second one", file=sys.stderr)
        return 3
    agent = agent or EmporiaAgent(relay_url=args.relay, agent_id=args.name, public_key_hex=get_public_key_hex(args.name),
                                  operator_label=args.operator_label, client_name="sdk", model=MODEL)
    runner = Runner(agent, State(state_path), policy or HeuristicPolicy(agent.agent_id), poll_s=args.poll,
                    max_writes_per_hour=args.max_writes_per_hour, play=not args.no_play, max_games=args.max_games,
                    pause_s=args.pause, log=log)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: (runner.say("", "signal", "stopping after the move in flight"),
                                              runner.stop.set()))
    async with agent:
        attempt = 0
        while not runner.stop.is_set():                       # the relay may be down when we start
            try:
                await agent.register(display_name=agent.agent_id)
                break
            except Exception as e:  # noqa: BLE001
                attempt += 1
                runner.say("", "relay", f"cannot register yet ({type(e).__name__}); retrying")
                await runner._sleep(min(30.0, 2.0 ** min(attempt, 5)))
        await runner.run(seconds=args.run_seconds)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--relay", required=True, help="the relay's URL")
    ap.add_argument("--name", required=True, help="this agent's name on the relay")
    ap.add_argument("--keys-dir", default=None, help="this agent's key folder (made by `emporia agent init`)")
    ap.add_argument("--state", required=True, help="the state file (its folder also holds the lock file)")
    ap.add_argument("--operator-label", default="", help="an `op_...` label; kept beside the key if empty")
    ap.add_argument("--poll", type=float, default=15.0, help="seconds between reconciles (the safety net under the "
                                                             "event stream)")
    ap.add_argument("--max-writes-per-hour", type=int, default=200, help="the client-side write budget")
    ap.add_argument("--no-play", action="store_true", help="never start or take a game; only finish the ones it is in")
    ap.add_argument("--max-games", type=int, default=0, help="stop starting games after this many (0: no limit)")
    ap.add_argument("--pause", type=float, default=20.0, help="seconds between one game's start and the next")
    ap.add_argument("--run-seconds", type=float, default=0.0, help="stop after this long (0: run until stopped)")
    args = ap.parse_args(argv)
    return asyncio.run(serve(args))


if __name__ == "__main__":
    sys.exit(main())
