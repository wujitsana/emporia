import httpx
import sys
sys.path.insert(0, "src")
from emporia.modules.chess import ChessModule
from emporia.modules.base import GameState

base = "http://127.0.0.1:8088"
with httpx.Client() as c:
    sessions = c.get(f"{base}/sessions").json()["sessions"]
    s = next(x for x in sessions if x["step_number"] == 7)
    sid = s["session_id"]
    actions = c.get(f"{base}/sessions/{sid}/actions").json()["actions"]
    print("sid", sid, "status", s["status"], "fen", s.get("state", {}).get("board_fen", "")[:50])
    print("last move", actions[-1]["payload"], "new_state keys", actions[-1].get("result", {}).get("new_state", {}).keys())
    st = GameState(data=actions[-1]["result"]["new_state"])
    over, out = ChessModule().is_terminal(st)
    print("is_terminal", over, out)