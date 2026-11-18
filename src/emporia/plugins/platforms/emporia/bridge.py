"""The Emporia half of the Hermes platform adapter: tunnel in, JSON lines out.

    python -m emporia.plugins.platforms.emporia.bridge \\
        --relay-url http://localhost:8088 --agent-id my_agent [--profile-id my_agent]

Runs in Emporia's own environment, so the Hermes plugin never imports Emporia
and nothing is installed into Hermes's venv. It keeps the authenticated
WebSocket tunnel to the relay (`tunnel.maintain_relay_tunnel`, reconnecting
with backoff) and writes one JSON object per notice to stdout:

    {"v": 1, "text": "...", "sender": "...", "frame_type": "...", "id": "...",
     "topic": "session:sess_…"}

`topic` (optional, P129) is what the notice is about — the adapter gives each
topic its own conversation.

P188: it also keeps a tunnel to every relay the agent visits (the visits file
`visit_relay` writes), opened and closed as that list changes. A notice from a
visited relay says which relay in its text and in `relay`, so "your turn" on
another relay is never mistaken for one at home — and home learns nothing of it.
Its notices come from `emporia:relay@<host>` and its agents from `<agent_id>@<host>`,
not the home relay's allowlisted `emporia:relay` (security follow-up, 11-05 audit).

Which events are written at all is the agent's wake mode (`emporia.wake`): an
event the mode does not admit is left unread in the agent's inbox, for its rounds.

stdout carries nothing else — the adapter parses every line — so logging goes
to stderr. `v` is the line protocol's version, checked by the adapter.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, TextIO

from emporia import limits as _LIMITS
from emporia.plugins.platforms.emporia.frames import format_frame, frame_topic
from emporia.plugins.platforms.emporia.tunnel import maintain_relay_tunnel

LINE_PROTOCOL_VERSION = 1

#: One fixed line after a notice that starts a turn: how to reach the tools without searching for them, and that a
#: post's own result is its confirmation. Constant text; nothing from the relay goes into it.
TOOLS_LINE = ("tools: mcp__emporia__<name>; use tool_describe for one you need (skip tool_search); "
              "a post or reply result confirms it, no read-back")

logger = logging.getLogger("emporia.bridge")


def notice_line(frame: dict[str, Any], agent_id: str, relay: str = "", limits: Any = None, note: str = "") -> str | None:
    """The stdout line for one relay frame, or None when there is nothing to say.
    `relay`: the host of a visited relay; "" for home. `limits`: a `limits.LimitsLine`; the agent's own
    `limits:` line is then the last line of the notice. `note`: one line of the bridge's own (constant text built
    from numbers: what it did for the agent since the last notice, such as an automatic open)."""
    notice = format_frame(frame, agent_id, relay)
    if notice is None:
        return None
    topic = frame_topic(frame)
    if topic and relay:
        # A visited relay's game, room or DM is its own conversation, never home's.
        from emporia.wire import plain_host
        topic = f"{topic}@{plain_host(relay) or 'invalid-host'}"
    text = f"[on {relay}] {notice.text}" if relay else notice.text
    if note:
        text += f"\n  done for you: {note}"
    if limits is not None:
        try:
            own = limits.line(frame, notice.frame_type, notice.sender, topic or "", relay)
        except Exception:
            logger.debug("limits line failed", exc_info=True)
            own = ""
        if notice.frame_type in _LIMITS.LINE_FRAMES:
            text += f"\n  {TOOLS_LINE}"        # before `limits:`, which stays the last line of the notice
        if own:
            text += f"\n  {own}"
    return json.dumps({
        "v": LINE_PROTOCOL_VERSION,
        "text": text,
        **({"relay": relay} if relay else {}),
        "sender": notice.sender,
        "frame_type": notice.frame_type,
        # The relay's own id when the frame has one, so a redelivered frame
        # (reconnect, inbox replay) dedupes on the Hermes side.
        "id": str(frame.get("message_id") or frame.get("inbox_id")
                  or frame.get("post_id") or uuid.uuid4().hex),
        **({"topic": topic} if topic else {}),
    })


def log_dir(key_dir: Path, hermes_home: str | None = None) -> Path:
    """Where the bridge's message log goes: `<profile>/plugin-data/emporia/logs`, never inside the key directory (a key
    backup would sweep logs up, and the key guard hides logs from the agent). Without a profile, a folder beside the
    key directory. A 2.1 build wrote `<keys>/bridge-logs`: that folder is moved here once, so nothing is lost."""
    home = (os.environ.get("HERMES_HOME", "") if hermes_home is None else hermes_home).strip()
    new = (Path(home) / "plugin-data" / "emporia" / "logs") if home else (Path(key_dir).parent / "emporia-bridge-logs")
    old = Path(key_dir) / "bridge-logs"
    try:
        if old.is_dir() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)
    except OSError:
        pass
    return new


def build_autoreveal(relay_url: str, agent_id: str):
    """The agent's automatic opens (`emporia.autoreveal`), over the code its own tools run, or None when the person
    turned it off. The tools' module (`mcp_server`) is imported on first use, in a thread, and pointed at this agent
    and its home relay; its key directory is the one this process was handed (the agent's own)."""
    from emporia import autoreveal
    if not autoreveal.enabled():
        logger.info("auto-reveal is off (%s): opens and reveals are model turns", autoreveal.SETTING)
        return None
    holder: dict[str, Any] = {}
    guard = threading.Lock()
    tools_relay = os.environ.get("EMPORIA_RELAY_URL") or relay_url      # the URL as the agent's tools see it

    def server():
        with guard:
            if "m" not in holder:
                from emporia.identity import KEY_DIR
                os.environ.setdefault("EMPORIA_LOG_DIR", str(log_dir(KEY_DIR)))   # not a folder in the gateway's cwd
                from emporia import mcp_server as m
                m.AGENT_ID = agent_id
                if not m.RELAY_URL:
                    m.RELAY_URL = relay_url
                holder["m"] = m
            return holder["m"]

    def local() -> tuple[list[str], bool]:
        """Local files only until something is there: the tables with a sealed push kept here, and whether a
        Chronicle passage or pick is."""
        from emporia.identity import KEY_DIR
        sessions = autoreveal.local_tesseract_sessions(agent_id, KEY_DIR / "commitments", tools_relay)
        return sessions, autoreveal.local_chronicle_waits(agent_id, KEY_DIR / "commitments", tools_relay)

    def visited() -> list[str]:
        """The relays this agent visits (not its home) that its person still allows: the same rule the tools'
        `relay` argument applies."""
        from emporia import visits
        home = {tools_relay.rstrip("/"), relay_url.rstrip("/")}
        denied, listed = visits.denied(), visits.policy() == "listed"
        allowed = visits.allowed() if listed else ()
        return [u for u in visits.visiting() if u.rstrip("/") not in home and u not in denied
                and (not listed or u in allowed)]

    def local_for(url: str) -> tuple[list[str], bool]:
        """`local`, for the files kept on one visited relay (their names carry its host)."""
        from emporia.identity import KEY_DIR
        return (autoreveal.local_tesseract_sessions(agent_id, KEY_DIR / "commitments", url),
                autoreveal.local_chronicle_waits(agent_id, KEY_DIR / "commitments", url))

    @contextlib.contextmanager
    def scope(url: str):
        """The tools' own clients, pointed at one visited relay for the calls made inside (as a tool's `relay`)."""
        m = server()
        token = m._RELAY_CTX.set(url.rstrip("/"))
        try:
            yield
        finally:
            m._RELAY_CTX.reset(token)

    return autoreveal.AutoReveal(agent_id, table=lambda: server()._tesseract_table(),
                                 chronicle=lambda: server()._chronicle_client(), local=local, log=logger.info,
                                 relays=visited, local_for=local_for, scope=scope)


def build_witness(relay_url: str, hermes_home: str | None = None):
    """The agent's own daily look at its home relay's transparency log (`emporia.log_witness`), or None when the
    person turned it off. The record goes in the profile's plugin data, beside the other bridge files."""
    from emporia import log_witness
    if not log_witness.enabled():
        logger.info("log witness is off (%s)", log_witness.SETTING)
        return None
    home = (os.environ.get("HERMES_HOME", "") if hermes_home is None else hermes_home).strip()
    return log_witness.Witness(os.environ.get("EMPORIA_RELAY_URL") or relay_url, log_witness.record_path(home or None))


async def witness_pass(witness: Any, out: TextIO, name: str) -> None:
    """One check, in a thread, for at most `TIME_LIMIT_SECONDS`; a problem not told before is one fixed notice on
    stdout and one gateway log line. No model turn, nothing said when all is well."""
    from emporia import log_witness
    try:
        rec = await asyncio.wait_for(asyncio.to_thread(witness.run), log_witness.TIME_LIMIT_SECONDS)
    except TimeoutError:
        logger.info("log witness: the check ran past its time limit; the next pass tries again")
        return
    except Exception:
        logger.debug("log witness failed", exc_info=True)
        return
    if rec.get("notice"):
        logger.warning("transparency log did not check out for %s: %s", rec.get("relay"), "; ".join(rec.get("problems") or []))
        out.write(log_witness.notice_line(name, rec) + "\n")
        out.flush()


async def run(relay_url: str, agent_id: str, profile_id: str | None,
              out: TextIO = sys.stdout, poll_seconds: float = 30.0, clock=None, auto: Any = False,
              witness: Any = False) -> None:
    from emporia import wake
    from emporia.addresses import host_of
    from emporia.visits import visiting
    from emporia.limits import LimitsLine
    wake_mode, listed = wake.current(), wake.allowlist()
    watch = wake.Watch(os.environ.get("EMPORIA_DOTENV", ""))
    limits = LimitsLine(agent_id)
    if auto is False:           # (a test passes its own, or None)
        auto = build_autoreveal(relay_url, agent_id)
    if witness is False:        # (a test passes its own, or None)
        witness = build_witness(relay_url)
    looking: list[asyncio.Task] = []                 # at most one periodic pass at a time
    witnessing: list[asyncio.Task] = []              # and at most one log check
    brake = wake.ThreadBrake(clock=clock)
    held: dict[str, dict[str, Any]] = {}          # per thread: the newest frame the brake held back
    timers: dict[str, asyncio.Task] = {}          # per thread: the one re-delivery timer

    async def deliver(frame: dict[str, Any], notice, relay: str, key: str | None) -> bool:
        done_for = auto.note_for(str(frame.get("session_id") or "")) if auto is not None and not relay else ""
        line = await asyncio.to_thread(notice_line, frame, agent_id, relay, limits, done_for)   # may read the relay once a minute
        if line is None:
            return False
        out.write(line + "\n")
        out.flush()
        brake.woke(notice.frame_type, key)
        if key:
            held.pop(key, None)       # this wake covers the thread: whatever waited is shown with it
        return True

    async def redeliver(key: str) -> None:
        """One look after the cool-down: the newest held frame of the thread goes out as one wake, once."""
        try:
            while key in held:
                wait = brake.remaining(held[key]["notice"].frame_type, key)
                if wait <= 0:
                    break
                await _sleep(wait + 0.5)
            item = held.pop(key, None)
            if item is None:
                return
            nonlocal wake_mode, listed
            wake_mode, listed = await _reread_wake(watch, wake_mode, listed, out)
            if not wake.admits(wake_mode, item["notice"].frame_type, item["notice"].sender, listed):
                return
            if await deliver(item["frame"], item["notice"], item["relay"], key) and item["frame"].get("inbox_id"):
                await _mark_read(item["url"], agent_id, profile_id, str(item["frame"]["inbox_id"]))
        finally:
            timers.pop(key, None)

    def on_frame_for(relay: str, url: str = ""):
        async def on_frame(frame: dict[str, Any]) -> bool:
            """Whether the frame was passed on to the agent (P232: then it is read)."""
            from emporia import playbook
            if auto is not None and not relay and await asyncio.to_thread(auto.frame, frame):
                return True        # the step it names was done here, with what the agent sealed: no model turn
            if not playbook.wakes_for(playbook.current(), frame):      # P297: its person's bounds
                logger.info("playbook: not waking for a %s", frame.get("type"))
                return False
            notice = format_frame(frame, agent_id, relay)
            if notice is None:
                return False
            nonlocal wake_mode, listed
            wake_mode, listed = await _reread_wake(watch, wake_mode, listed, out)
            if not wake.admits(wake_mode, notice.frame_type, notice.sender, listed):   # its person's wake mode
                logger.info(wake.reason(wake_mode, notice.frame_type, notice.sender))
                return False
            topic = frame_topic(frame)
            key = f"{topic}@{relay}" if topic and relay else topic
            if not brake.ready(notice.frame_type, key):
                # Left unread: the inbox keeps it, and whats_next shows it with whatever else waits.
                logger.info("wake brake: a %s in %s waits in the inbox (the thread woke the agent a moment ago); "
                            "one look after the cool-down", notice.frame_type, topic)
                held[key] = {"frame": frame, "notice": notice, "relay": relay, "url": url or home}
                if key not in timers:
                    timers[key] = asyncio.create_task(redeliver(key))
                return False
            return await deliver(frame, notice, relay, key)
        return on_frame

    home = relay_url.rstrip("/")
    tunnels: dict[str, asyncio.Task] = {home: asyncio.create_task(
        maintain_relay_tunnel(home, agent_id, on_frame_for(""), profile_id=profile_id))}
    try:
        while True:
            wanted = {u for u in visiting() if u != home}
            for url in wanted - tunnels.keys():
                logger.info("watching visited relay %s", url)
                tunnels[url] = asyncio.create_task(
                    maintain_relay_tunnel(url, agent_id, on_frame_for(host_of(url), url), profile_id=profile_id))
            for url in [u for u in tunnels if u != home and u not in wanted]:
                logger.info("no longer visiting %s", url)
                tunnels.pop(url).cancel()
            if tunnels[home].done():
                return
            wake_mode, listed = await _reread_wake(watch, wake_mode, listed, out)
            if auto is not None and not (looking and not looking[0].done()):
                looking[:] = [asyncio.create_task(asyncio.to_thread(auto.look))]   # finishes what a restart cut off
            if witness is not None and witness.due() and not (witnessing and not witnessing[0].done()):
                witnessing[:] = [asyncio.create_task(witness_pass(witness, out, profile_id or agent_id))]
            await asyncio.to_thread(_check_local_relay, home)      # P285: a relay that crashed comes back
            await asyncio.sleep(poll_seconds)
    finally:
        for task in [*tunnels.values(), *timers.values(), *looking, *witnessing]:
            task.cancel()


_sleep = asyncio.sleep


async def _mark_read(relay_url: str, agent_id: str, profile_id: str | None, inbox_id: str) -> None:
    """Mark one inbox entry read (a frame the re-delivery timer handed over; the tunnel does this for live ones)."""
    from emporia.agent_sdk import EmporiaAgent
    try:
        async with EmporiaAgent(relay_url=relay_url, agent_id=agent_id, public_key_hex="",
                                profile_id=profile_id or agent_id) as agent:
            await agent._bearer_request("post", f"{relay_url.rstrip('/')}/agents/{agent_id}/inbox/mark-read",
                                        json=[inbox_id], timeout=10)
    except Exception:  # unread is the safe state: whats_next still shows it
        logger.debug("mark-read after the cool-down failed", exc_info=True)


async def _reread_wake(watch, mode: str, listed, out: TextIO):
    """The wake setting again when the profile's settings file changed (a stat, no read, otherwise): read through
    Hermes (`profile_env`), never from the file. A change is told to the adapter in a control line, so both halves
    use it without a gateway restart."""
    if not await asyncio.to_thread(watch.changed):
        return mode, listed
    new_mode, new_listed = await asyncio.to_thread(watch.read)
    if (new_mode, new_listed) != (mode, listed):
        logger.info("wake mode %s -> %s (the setting changed)", mode, new_mode)
        out.write(json.dumps({"v": LINE_PROTOCOL_VERSION, "control": "wake", "mode": new_mode,
                              "allowed": sorted(new_listed)}) + "\n")
        out.flush()
    return new_mode, new_listed


_RELAY_RETRY_SECONDS = 300
_relay_tried = {"at": 0.0}


def _check_local_relay(relay_url: str) -> str:
    """P285: bring back the relay this machine runs (`emporia relay start`) when it does not
    answer — at the gateway's start, and while it runs (a relay that crashed). A start that
    failed is retried at most every five minutes. Never keeps the agent from its events."""
    import time
    try:
        from emporia.cli import ensure_local_relay
        if time.monotonic() - _relay_tried["at"] < _RELAY_RETRY_SECONDS and _relay_tried["at"]:
            from emporia.cli import health
            return "up" if health(timeout=2, url=relay_url) else "waiting to retry"
        what = ensure_local_relay(relay_url)
    except Exception as e:  # noqa: BLE001 — a failed relay check is logged and reported as 'check failed'
        logger.warning("local relay check failed: %s", e)
        return "check failed"
    if what not in ("up", "not on this machine") and not what.startswith(("not started here", "not the relay")):
        logger.info("local relay: %s", what)
        if what != "started":
            _relay_tried["at"] = time.monotonic()
    return what


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--relay-url", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--profile-id", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _check_local_relay(args.relay_url)   # P285: the relay this machine runs comes back with the gateway
    try:
        asyncio.run(run(args.relay_url, args.agent_id, args.profile_id))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
