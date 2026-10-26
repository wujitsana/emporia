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
        # P64: `--inexact` — see installer/install.py's matching call. A bare
        # `uv sync` prunes the `dev` and `x402` extras out of a contributor's
        # venv, and this one runs on every relay start.
        subprocess.run(
            [uv, "sync", "--inexact", "--directory", str(EMPORIA_ROOT)],
            cwd=str(EMPORIA_ROOT), check=False,
        )
    else:
        subprocess.run(
            [venv_python(), "-m", "pip", "install", "-e", str(EMPORIA_ROOT)],
            cwd=str(EMPORIA_ROOT),
            check=False,
        )


def _pid_file() -> Path:
    """Where `emporia relay start` keeps its pid, so `emporia relay stop` stops
    a relay started here too: the profile holding this checkout, else the repo."""
    for parent in EMPORIA_ROOT.parents:
        if (parent / "config.yaml").exists():
            return parent / "home" / ".hermes" / "emporia-relay.pid"
    return EMPORIA_ROOT / "home" / ".hermes" / "emporia-relay.pid"


def start_relay(relay_url: str) -> bool:
    """Start the relay in the background. Returns True if /health within ~12s.
    It listens on EMPORIA_RELAY_HOST — 127.0.0.1, this machine only, unless set."""
    if not is_local_relay(relay_url):
        return False
    _sync_venv_deps()
    port = urlparse(relay_url).port or 8088
    env = {**os.environ, "EMPORIA_RELAY_PORT": str(port)}
    log_path = EMPORIA_ROOT / ".relay.log"
    log = open(log_path, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [venv_python(), "-m", "emporia.relay_server"],
        cwd=str(EMPORIA_ROOT), env=env, stdout=log, stderr=log,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    pid_file = _pid_file()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(proc.pid))
    for _ in range(12):
        time.sleep(1)
        if relay_healthy(relay_url, timeout=2):
            print(f"  Relay started on {relay_url} (pid {proc.pid}, log: {log_path}; stop: emporia relay stop)")
            return True
        if proc.poll() is not None:
            break
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