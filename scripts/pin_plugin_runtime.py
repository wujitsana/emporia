"""Pin the Emporia runtime the Hermes plugin installs to a commit (P295).

    .venv/bin/python scripts/pin_plugin_runtime.py            # pin to HEAD
    .venv/bin/python scripts/pin_plugin_runtime.py <sha>      # pin to a given commit

A release is two commits:
1. **The code.** It must be pushed: the catalog clones it from GitHub.
2. **This file's change** (`hermes_plugin/emporia/emporia_source.json` naming commit 1).

The catalog entry pins commit 2. The pinned commit is then one a reviewer can read, and it never names itself.
Refuses a dirty tree, and a commit `origin` does not have.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "hermes_plugin" / "emporia" / "emporia_source.json"


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True).stdout.strip()


def main(argv: list[str]) -> int:
    if git("status", "--porcelain", "--untracked-files=no"):
        print("pin: the tree has uncommitted changes; commit them first (they are what gets pinned)")
        return 1
    sha = git("rev-parse", argv[0] if argv else "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        print(f"pin: {sha!r} is not a full commit")
        return 1
    on_origin = subprocess.run(["git", "-C", str(REPO), "branch", "-r", "--contains", sha],
                               capture_output=True, text=True).stdout.strip()
    if not on_origin:
        print(f"pin: origin has no branch containing {sha[:12]} — push it first, or the catalog's clone fails")
        return 1
    data = json.loads(SOURCE.read_text())
    data["sha"] = sha
    SOURCE.write_text(json.dumps(data, indent=2) + "\n")
    print(f"pinned the plugin's runtime to {sha}. Commit this file; the catalog entry pins that commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
