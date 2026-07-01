"""Start a local Emporia relay when seed/install needs one (localhost only)."""

from __future__ import annotations

import os
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

EMPORIA_ROOT = Path(__file__).resolve().parent.parent


def is_local_relay(relay_url: str) -> bool:
    host = (urlparse(relay_url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "")


def relay_healthy(relay_url: str, timeout: float = 3) -> bool:
    try:
        urllib.request.urlopen(f"{relay_url.rstrip('/')}/health", timeout=timeout)
        return True
    except Exception:
        return False


def venv_python() -> str:
    p = EMPORIA_ROOT / ".venv" / "bin" / "python"
    return str(p) if p.is_file() else os.environ.get("PYTHON", "python3")


def _sync_venv_deps() -> None:
    import shutil

    uv = shutil.which("uv")
    if uv:
        subprocess.run([uv, "sync", "--directory", str(EMPORIA_ROOT)], cwd=str(EMPORIA_ROOT), check=False)
    else:
        subprocess.run(
            [venv_python(), "-m", "pip", "install", "-e", str(EMPORIA_ROOT)],
            cwd=str(EMPORIA_ROOT),
            check=False,
        )


def start_relay(relay_url: str) -> bool:
    """Start uvicorn in the background. Returns True if /health within ~12s."""
    if not is_local_relay(relay_url):
        return False
    _sync_venv_deps()
    port = relay_url.rstrip("/").rsplit(":", 1)[-1] if ":" in relay_url else "8088"
    src = EMPORIA_ROOT / "src"
    env = os.environ.copy()
    log_path = EMPORIA_ROOT / ".relay.log"
    log = open(log_path, "w", encoding="utf-8")
    subprocess.Popen(
        [
            venv_python(),
            "-m",
            "uvicorn",
            "emporia.relay_server:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--app-dir",
            str(src),
        ],
        cwd=str(EMPORIA_ROOT),
        env=env,
        stdout=log,
        stderr=log,
    )
    for _ in range(12):
        time.sleep(1)
        if relay_healthy(relay_url, timeout=2):
            print(f"  Relay started on {relay_url} (log: {log_path})")
            return True
    print(f"  Relay did not become healthy — see {log_path}")
    return False


def ensure_relay_running(relay_url: str) -> bool:
    if relay_healthy(relay_url):
        return True
    if not is_local_relay(relay_url):
        print(f"  Relay not reachable at {relay_url} (remote URL — start relay manually)")
        return False
    print(f"  Relay not running — starting on {relay_url}…")
    return start_relay(relay_url)