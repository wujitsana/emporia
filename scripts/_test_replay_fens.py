import httpx
import chess

base = "http://127.0.0.1:8088"
with httpx.Client() as c:
    sessions = c.get(f"{base}/sessions").json()["sessions"]
    chess_sess = [s for s in sessions if "chess" in s["module_type"] and s.get("step_number", 0) > 0]
    chess_sess.sort(key=lambda x: -x.get("step_number", 0))
    s = chess_sess[0]
    acts = c.get(f"{base}/sessions/{s['session_id']}/actions").json()["actions"]
    board = chess.Board()
    fens = [board.fen()]
    for a in acts:
        if a["action_type"] != "move":
            continue
        uci = a["payload"].get("uci") or a["payload"].get("move")
        board.push(chess.Move.from_uci(uci))
        fens.append(board.fen())
    print(s["session_id"][-8], "steps", s["step_number"], "unique_fens", len(set(fens)), "frames", len(fens))