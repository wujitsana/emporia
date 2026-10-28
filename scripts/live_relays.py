#!/usr/bin/env python3
"""Run the live suite (P248): real relay processes on this machine, talking over HTTP.

Each scenario starts its own relays (A and B linked both ways, C a stranger, a fourth that pulls from a
hostile operator's hop), agents with key folders of their own, and tears them down. Ports are picked
free at run time and every folder is a temporary one, so it is safe to run beside another run.

Usage:
    python scripts/live_relays.py                 # every scenario, a few minutes
    python scripts/live_relays.py -k rotation     # only scenarios whose name matches
    python scripts/live_relays.py --keep DIR      # keep every relay's database, keys and log under DIR
    python scripts/live_relays.py --list          # name the scenarios and stop

Anything else is passed to pytest. On a failure, each relay's and agent's log tail is printed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    args = ["-m", "live", "tests/live", "-q"]
    rest = list(argv)
    if "--list" in rest:
        rest.remove("--list")
        args += ["--collect-only"]
    if "--keep" in rest:
        i = rest.index("--keep")
        if i + 1 >= len(rest):
            print("--keep needs a folder, like --keep /tmp/live-run")
            return 2
        args += ["--basetemp", str(Path(rest[i + 1]).expanduser().resolve())]
        del rest[i:i + 2]
    cmd = [sys.executable, "-m", "pytest", *args, *rest]
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
