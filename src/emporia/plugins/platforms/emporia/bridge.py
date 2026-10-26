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
        async def on_frame(frame: dict[str, Any]) -> None:
            line = notice_line(frame, agent_id, relay)
            if line is not None:
                out.write(line + "\n")
                out.flush()
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
            await asyncio.sleep(poll_seconds)
    finally:
        for task in tunnels.values():
            task.cancel()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--relay-url", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--profile-id", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(run(args.relay_url, args.agent_id, args.profile_id))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
