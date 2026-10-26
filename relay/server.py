"""Thin entry point for the Emporia relay server.

Run:
    python relay/server.py
    EMPORIA_RELAY_PORT=8088 python relay/server.py
    uvicorn emporia.relay_server:app --host 127.0.0.1 --port 8088
"""

import sys
from pathlib import Path

_src = str(Path(__file__).parent.parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from emporia.relay_server import main

if __name__ == "__main__":
    main()
