"""Live-test helper: a thread id between relays is opaque (random, chosen by whoever began it), so a test
asks the agent's own relay for it instead of computing it from the two addresses."""
from __future__ import annotations


def thread_id_for(agent, other: str) -> str:
    """The id of `agent`'s thread with `other` (an address), as its own relay lists it."""
    out = agent.tool("list_dm_threads")
    for t in out.get("threads", out if isinstance(out, list) else []):
        if other in (t.get("agent_a"), t.get("agent_b"), t.get("peer_label"), t.get("with")):
            return t["thread_id"]
    raise AssertionError(f"{agent.address} has no thread with {other}: {out}")
