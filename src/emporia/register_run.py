"""What `emporia register` and setup run as the agent: register it, then each wallet it has as where it is
paid. A plain function (not generated code) so an agent with no wallet cannot produce a broken script."""
import json


def run(agent_id: str, families=()) -> dict:
    """Register `agent_id`, print one RESULT line (the CLI reads it), then one line per wallet family."""
    from emporia import mcp_server as m
    r = m.register_agent(agent_id=agent_id)
    ok = bool(r.get("success"))
    print("RESULT " + json.dumps({"ok": ok, "who": str(r.get("address") or r.get("agent_id") or ""),
                                  "error": str(r.get("error") or r.get("detail") or "")[:300]}))
    if ok:
        for f in families:
            w = m.register_x402_wallet(agent_id=agent_id, network_family=f)
            print(f + " wallet", "registered" if w.get("success") else w.get("error") or w.get("detail"))
    return r
