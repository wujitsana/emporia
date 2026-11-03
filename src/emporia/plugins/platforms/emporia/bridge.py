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

stdout carries nothing else — the adapter parses every line — so logging goes
to stderr. `v` is the line protocol's version, checked by the adapter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from typing import Any, TextIO

from emporia.plugins.platforms.emporia.frames import format_frame, frame_topic
from emporia.plugins.platforms.emporia.tunnel import maintain_relay_tunnel

LINE_PROTOCOL_VERSION = 1

logger = logging.getLogger("emporia.bridge")


def notice_line(frame: dict[str, Any], agent_id: str, relay: str = "") -> str | None:
    """The stdout line for one relay frame, or None when there is nothing to say.
    `relay`: the host of a visited relay; "" for home."""
    notice = format_frame(frame, agent_id)
    if notice is None:
        return None
    topic = frame_topic(frame)
    return json.dumps({
        "v": LINE_PROTOCOL_VERSION,
        "text": f"[on {relay}] {notice.text}" if relay else notice.text,
        **({"relay": relay} if relay else {}),
        "sender": notice.sender,
        "frame_type": notice.frame_type,
        # The relay's own id when the frame has one, so a redelivered frame
        # (reconnect, inbox replay) dedupes on the Hermes side.
        "id": str(frame.get("message_id") or frame.get("inbox_id")
                  or frame.get("post_id") or uuid.uuid4().hex),
        **({"topic": topic} if topic else {}),
    })


async def run(relay_url: str, agent_id: str, profile_id: str | None,
              out: TextIO = sys.stdout, poll_seconds: float = 30.0) -> None:
    from emporia.addresses import host_of
    from emporia.visits import visiting

    def on_frame_for(relay: str):
        async def on_frame(frame: dict[str, Any]) -> bool:
            """Whether the frame was passed on to the agent (P232: then it is read)."""
            line = notice_line(frame, agent_id, relay)
            if line is None:
                return False
            out.write(line + "\n")
            out.flush()
            return True
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
                    maintain_relay_tunnel(url, agent_id, on_frame_for(host_of(url)), profile_id=profile_id))
            for url in [u for u in tunnels if u != home and u not in wanted]:
                logger.info("no longer visiting %s", url)
                tunnels.pop(url).cancel()
            if tunnels[home].done():
                return
            await asyncio.to_thread(_check_local_relay, home)      # P285: a relay that crashed comes back
            await asyncio.sleep(poll_seconds)
    finally:
        for task in tunnels.values():
            task.cancel()


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
    except Exception as e:
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
