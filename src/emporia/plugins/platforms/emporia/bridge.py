"""The Emporia half of the Hermes platform adapter: tunnel in, JSON lines out.

    python -m emporia.plugins.platforms.emporia.bridge \\
        --relay-url http://localhost:8088 --agent-id my_agent [--profile-id my_agent]

Runs in Emporia's own environment, so the Hermes plugin never imports Emporia
and nothing is installed into Hermes's venv. It keeps the authenticated
WebSocket tunnel to the relay (`tunnel.maintain_relay_tunnel`, reconnecting
with backoff) and writes one JSON object per notice to stdout:

    {"v": 1, "text": "...", "sender": "...", "frame_type": "...", "id": "..."}

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

from emporia.plugins.platforms.emporia.frames import format_frame
from emporia.plugins.platforms.emporia.tunnel import maintain_relay_tunnel

LINE_PROTOCOL_VERSION = 1

logger = logging.getLogger("emporia.bridge")


def notice_line(frame: dict[str, Any], agent_id: str) -> str | None:
    """The stdout line for one relay frame, or None when there is nothing to say."""
    notice = format_frame(frame, agent_id)
    if notice is None:
        return None
    return json.dumps({
        "v": LINE_PROTOCOL_VERSION,
        "text": notice.text,
        "sender": notice.sender,
        "frame_type": notice.frame_type,
        # The relay's own id when the frame has one, so a redelivered frame
        # (reconnect, inbox replay) dedupes on the Hermes side.
        "id": str(frame.get("message_id") or frame.get("inbox_id")
                  or frame.get("post_id") or uuid.uuid4().hex),
    })


async def run(relay_url: str, agent_id: str, profile_id: str | None,
              out: TextIO = sys.stdout) -> None:
    async def on_frame(frame: dict[str, Any]) -> None:
        line = notice_line(frame, agent_id)
        if line is not None:
            out.write(line + "\n")
            out.flush()

    await maintain_relay_tunnel(relay_url, agent_id, on_frame, profile_id=profile_id)


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
