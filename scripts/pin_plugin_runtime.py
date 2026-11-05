"""Pin the Emporia runtime the Hermes plugin installs to a commit (P295).

    .venv/bin/python scripts/pin_plugin_runtime.py            # pin to HEAD
    .venv/bin/python scripts/pin_plugin_runtime.py <sha>      # pin to a given commit
    .venv/bin/python scripts/pin_plugin_runtime.py --export   # write installer/runtime-requirements.txt

A release is two commits:
1. **The code.** It must be pushed: the catalog clones it from GitHub.
2. **This file's change** (`hermes_plugin/emporia/emporia_source.json` naming commit 1).

The catalog entry pins commit 2. The pinned commit is then one a reviewer can read, and it never names itself.
Refuses a dirty tree, and a commit `origin` does not have.

Security follow-up (11-05 audit): without uv, setup installs the runtime with pip from
`installer/runtime-requirements.txt` — uv.lock exported with every file's hash (no dev
dependencies, the x402 extra). `--export` writes it; commit it with the code. Pinning refuses a
commit whose committed export is not what its own uv.lock exports, so the two stay in step.
Needs uv (`uv export --frozen`: offline, reads only the lock).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "hermes_plugin" / "emporia" / "emporia_source.json"
REQUIREMENTS_PATH = "installer/runtime-requirements.txt"
REQUIREMENTS = REPO / REQUIREMENTS_PATH
_EXPORT = ["export", "--frozen", "--no-dev", "--extra", "x402", "--no-emit-project", "--no-header",
           "--format", "requirements-txt"]


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True).stdout.strip()


def _on_origin(sha: str) -> bool:
    return bool(subprocess.run(["git", "-C", str(REPO), "branch", "-r", "--contains", sha],
                               capture_output=True, text=True).stdout.strip())


def _uv() -> str:
    uv = os.environ.get("EMPORIA_UV") or shutil.which("uv")
    if not uv:
        raise SystemExit("pin: uv is needed to export uv.lock (set EMPORIA_UV to its path if it is not on PATH)")
    return uv


def export_requirements(sha: str | None = None) -> str:
    """uv.lock exported for pip, with hashes — this tree's, or the commit `sha`'s."""
    if not sha:
        return _export_in(REPO)
    with tempfile.TemporaryDirectory() as d:
        for name in ("pyproject.toml", "uv.lock", "README.md"):
            shown = subprocess.run(["git", "-C", str(REPO), "show", f"{sha}:{name}"],
                                   capture_output=True, check=False)
            if shown.returncode == 0:
                (Path(d) / name).write_bytes(shown.stdout)
        return _export_in(Path(d))


def _export_in(root: Path) -> str:
    out = subprocess.run([_uv(), *_EXPORT], cwd=root, capture_output=True, text=True,
                         env={**os.environ, "UV_OFFLINE": "1"})
    if out.returncode != 0:
        raise SystemExit(f"pin: uv export failed: {out.stderr.strip()[-400:]}")
    return out.stdout


def committed_requirements(sha: str) -> str | None:
    shown = subprocess.run(["git", "-C", str(REPO), "show", f"{sha}:{REQUIREMENTS_PATH}"],
                           capture_output=True, text=True)
    return shown.stdout if shown.returncode == 0 else None


def main(argv: list[str]) -> int:
    if argv[:1] == ["--export"]:
        REQUIREMENTS.write_text(export_requirements())
        print(f"wrote {REQUIREMENTS_PATH} from uv.lock. Commit it with the code it goes with.")
        return 0
    if git("status", "--porcelain", "--untracked-files=no"):
        print("pin: the tree has uncommitted changes; commit them first (they are what gets pinned)")
        return 1
    sha = git("rev-parse", argv[0] if argv else "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        print(f"pin: {sha!r} is not a full commit")
        return 1
    if not _on_origin(sha):
        print(f"pin: origin has no branch containing {sha[:12]} — push it first, or the catalog's clone fails")
        return 1
    if committed_requirements(sha) != export_requirements(sha):
        print(f"pin: {REQUIREMENTS_PATH} at {sha[:12]} is out of step with uv.lock (or missing) — run "
              "`scripts/pin_plugin_runtime.py --export`, commit and push it, then pin that commit")
        return 1
    data = json.loads(SOURCE.read_text())
    data["sha"] = sha
    SOURCE.write_text(json.dumps(data, indent=2) + "\n")
    print(f"pinned the plugin's runtime to {sha}. Commit this file; the catalog entry pins that commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
