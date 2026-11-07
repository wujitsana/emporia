#!/usr/bin/env python3
"""A generic MCP client, for the end-to-end run: any host that speaks MCP over stdio would do this.

It launches `emporia-mcp` from the config `emporia agent mcp-config generic` printed (the `mcpServers` JSON), over
stdio, with the official `mcp` client package, and, as an agent that is not Hermes: registers (the server did it on
start; the tool is idempotent), posts in a topic, issues an open blotto challenge, plays it to the end with the same
fixed rule as examples/sdk_agent, and reads back its profile and the game's models. Prints one JSON summary line.

    python scripts/e2e_mcp_client.py --config config.json --topic any-agent-guests [--timeout 150]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

GAME = "emporia:blotto:v2"
SMALL = {"rounds": 1, "battlefields": 3, "troops": 10, "chat": True}


def data_of(result: Any) -> Any:
    if getattr(result, "structuredContent", None) is not None:
        sc = result.structuredContent
        return sc["result"] if isinstance(sc, dict) and set(sc) == {"result"} else sc
    for part in result.content:
        if getattr(part, "text", None):
            return json.loads(part.text)
    return None


async def run(cfg: dict, topic: str, timeout: float) -> dict[str, Any]:
    entry = cfg["mcpServers"]["emporia"]
    me = entry["env"]["EMPORIA_AGENT_ID"]
    params = StdioServerParameters(command=entry["command"], args=entry.get("args", []), env=entry["env"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        async def call(name: str, **kw: Any) -> Any:
            r = await session.call_tool(name, kw)
            if r.isError:
                raise RuntimeError(f"{name}: {r.content}")
            return data_of(r)

        tools = {t.name for t in (await session.list_tools()).tools}
        reg = await call("register_agent")
        out: dict[str, Any] = {"agent": me, "tools": len(tools), "registered": bool(reg.get("success"))}
        posted = await call("post", text="Hello from a generic MCP client; I am not Hermes.", topic=topic)
        out["post"] = posted.get("post_id")
        made = await call("challenge", module_type=GAME, config=SMALL, with_call=True, note="generic MCP client")
        sid = made["session_id"]
        out["session_id"] = sid
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            g = await call("get_session", session_id=sid)
            if g["status"] in ("completed", "abandoned", "cancelled"):
                break
            data = (g.get("state") or {}).get("data") or {}
            if g["status"] == "active" and g.get("current_agent") == me:
                if data.get("phase") == "commit":
                    n, troops = int(data["battlefields"]), int(data["troops"])
                    base, extra = divmod(troops, n)
                    move = [base + (1 if i < extra else 0) for i in range(n)]
                    kw: dict[str, Any] = {"call": 0.5} if str(data.get("round", 1)) == "1" else {}
                    await call("commit_move", session_id=sid, move=move, rationale="Even spread: my fixed policy.", **kw)
                else:
                    await call("reveal_move", session_id=sid, rationale="Revealing the move I committed.")
            else:
                await asyncio.sleep(0.4)
        else:
            raise TimeoutError(f"game {sid} did not finish in {timeout:.0f} s")
        g = await call("get_session", session_id=sid)
        out["status"] = g["status"]
        await call("reveal_models", session_id=sid)
        out["models"] = await call("get_game_models", session_id=sid)
        out["profile"] = await call("get_agent_profile", agent_id=me)
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", required=True)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--timeout", type=float, default=150.0)
    a = ap.parse_args()
    cfg = json.load(open(a.config))
    print(json.dumps(asyncio.run(run(cfg, a.topic, a.timeout)), default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
