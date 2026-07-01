import httpx
import json

base = "http://127.0.0.1:8088"
with httpx.Client() as c:
    sessions = c.get(f"{base}/sessions").json()["sessions"]
    s = max(sessions, key=lambda x: x.get("step_number", 0))
    sid = s["session_id"]
    acts = c.get(f"{base}/sessions/{sid}/actions").json()["actions"]
    print("n", len(acts))
    for i, a in enumerate(acts[:3]):
        r = a.get("result") or {}
        ns = r.get("new_state") or {}
        print(i, a.get("payload"), "fen" in ns, ns.get("board_fen", "")[:40] if ns else "NO_NS")
    fens = []
    prev = None
    for a in acts:
        fen = (a.get("result") or {}).get("new_state", {}).get("board_fen")
        if fen:
            fens.append(fen)
    print("unique fens", len(set(fens)), "total", len(fens))