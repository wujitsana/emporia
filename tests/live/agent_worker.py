"""One agent, as a process of its own (P248): its key folder, its relay, its
environment. The test sends it Python lines and reads back what each left in `r`.

Run as `python agent_worker.py`; the protocol is one JSON object per line on stdin
({"code": "..."}) and one JSON object per line on the real stdout ({"r": ...} or
{"error": "..."}). Whatever the agent's own code prints goes to stderr, so it
cannot be mistaken for an answer.
"""
import json
import os
import sys
import traceback

answers = os.fdopen(os.dup(1), "w", buffering=1)
sys.stdout = sys.stderr

import httpx

from emporia import mcp_server as m

namespace = {"m": m, "json": json, "os": os, "httpx": httpx}
for line in sys.stdin:
    if not line.strip():
        continue
    try:
        exec(json.loads(line)["code"], namespace)
        text = json.dumps({"r": namespace.pop("r", None)}, default=str)
    except BaseException:  # noqa: BLE001 - report it, keep serving
        text = json.dumps({"error": traceback.format_exc()[-2500:]})
    answers.write(text + "\n")
