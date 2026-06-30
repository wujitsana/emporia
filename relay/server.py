"""Thin entry point for the Emporia relay server.

Run:
    python relay/server.py
    EMPORIA_RELAY_PORT=8088 python relay/server.py
    uvicorn emporia.relay_server:app --host 0.0.0.0 --port 8088
"""

import sys
from pathlib import Path

_src = str(Path(__file__).parent.parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

import os
import uvicorn
from emporia.relay_server import app

if __name__ == "__main__":
    port = int(os.getenv("EMPORIA_RELAY_PORT", "8088"))
    uvicorn.run(app, host="0.0.0.0", port=port)
