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

import httpx  # noqa: E402

from emporia import mcp_server as m  # noqa: E402

namespace = {"m": m, "json": json, "os": os, "httpx": httpx}
for line in sys.stdin:
    if not line.strip():
        continue
    try:
        exec(json.loads(line)["code"], namespace)   # noqa: S102 - the test's own code, in the test's own process
        text = json.dumps({"r": namespace.pop("r", None)}, default=str)
    except BaseException:  # noqa: BLE001 - report it, keep serving
        text = json.dumps({"error": traceback.format_exc()[-2500:]})
    answers.write(text + "\n")
