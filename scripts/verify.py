#!/usr/bin/env python3
"""One command that checks the whole repository, in order, with a pass or fail line for each step.

    python scripts/verify.py [--e2e] [--skip NAME ...]

Steps (names for --skip):

* pytest      the full unit suite (`python -m pytest -q`)
* portable    `scripts/build_portable_skill.py --check`: the portable skill is the plugin's skill, rebuilt
* dashboard   `npm run typecheck`, `npm run lint` and `npm run test` (vitest) in dashboard/; skipped, with a note,
              when npm or the dashboard's node_modules is not there
* plugin      `hermes plugins validate` on a `git archive` of HEAD's plugin folder, in a throwaway HERMES_HOME and
              HOME; a warning counts as a failure; skipped, with a note, when `hermes` is not on PATH
* e2e         only with --e2e: `scripts/e2e.py`, the scripted end-to-end run (about 18 minutes, installs the
              plugin and runtime from HEAD, so commit first)

A step that is skipped is not a pass: the summary says so. The exit code is 0 only when nothing failed. Standard
library only, so it runs before anything is installed; it uses the repo's `.venv` when there is one.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard"
STEPS = ("pytest", "portable", "dashboard", "plugin", "e2e")
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def python() -> str:
    venv = ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def child_env(**extra: str) -> dict[str, str]:
    """No profile .env: a check never reads, or writes to, a person's profile."""
    return {**os.environ, "EMPORIA_NO_DOTENV": "1", **extra}


def run(argv: list[str], cwd: Path = ROOT, env: dict[str, str] | None = None, timeout: float = 7200) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, cwd=cwd, env=env or child_env(), stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s: {' '.join(argv[:4])}"
    except OSError as e:
        return 127, f"could not run {argv[0]}: {e}"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def tail(text: str, n: int = 12) -> str:
    return "\n".join("      " + line for line in text.strip().splitlines()[-n:])


def step_pytest() -> tuple[str, str, str]:
    code, out = run([python(), "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rf", str(ROOT / "tests")])
    summary = next((ln for ln in reversed(out.splitlines()) if " passed" in ln or " failed" in ln or "error" in ln), "")
    return (PASS if code == 0 else FAIL), summary.strip(" =") or f"exit {code}", "" if code == 0 else tail(out, 25)


def step_portable() -> tuple[str, str, str]:
    code, out = run([python(), str(ROOT / "scripts" / "build_portable_skill.py"), "--check"])
    return (PASS if code == 0 else FAIL), ("up to date" if code == 0 else "out of date: run it without --check"), \
        "" if code == 0 else tail(out)


def step_dashboard() -> tuple[str, str, str]:
    npm = shutil.which("npm")
    if not npm:
        return SKIP, "npm is not on PATH", ""
    if not (DASHBOARD / "node_modules").exists():
        return SKIP, "dashboard/node_modules is missing (run `npm ci` there once)", ""
    done = []
    for script in ("typecheck", "lint", "test"):
        code, out = run([npm, "--prefix", str(DASHBOARD), "run", script], cwd=DASHBOARD, timeout=1800)
        if code != 0:
            return FAIL, f"`npm run {script}` failed", tail(out, 25)
        done.append(script)
    return PASS, " + ".join(done), ""


def step_plugin() -> tuple[str, str, str]:
    hermes = shutil.which("hermes")
    if not hermes:
        return SKIP, "hermes is not on PATH", ""
    with tempfile.TemporaryDirectory(prefix="emporia-verify-") as tmp:
        base = Path(tmp)
        tar = base / "head.tar"
        code, out = run(["git", "-C", str(ROOT), "archive", "--format=tar", "--output", str(tar), "HEAD",
                         "hermes_plugin"])
        if code != 0:
            return FAIL, "git archive HEAD failed", tail(out)
        (base / "src").mkdir()
        code, out = run(["tar", "-xf", str(tar), "-C", str(base / "src")])
        if code != 0:
            return FAIL, "could not unpack the archive", tail(out)
        (base / "hh").mkdir()
        (base / "home").mkdir()
        code, out = run([hermes, "plugins", "validate", str(base / "src" / "hermes_plugin" / "emporia")],
                        env=child_env(HERMES_HOME=str(base / "hh"), HOME=str(base / "home")), timeout=300)
    warned = [ln for ln in out.splitlines() if ln.lstrip().startswith("⚠")]
    if code != 0 or "Validation passed" not in out:
        return FAIL, "validation failed", tail(out, 25)
    if warned:
        return FAIL, f"{len(warned)} warning(s)", "\n".join("      " + w for w in warned)
    return PASS, "Validation passed, no warnings (of HEAD, so commit first)", ""


def step_e2e() -> tuple[str, str, str]:
    code, out = run([python(), str(ROOT / "scripts" / "e2e.py"), "--repo", str(ROOT)], timeout=3 * 3600)
    last = [ln for ln in out.splitlines() if ln.startswith(("✓", "✗"))]
    return (PASS if code == 0 else FAIL), (f"{len(last)} steps, last: {last[-1]}" if last else f"exit {code}"), \
        "" if code == 0 else tail(out, 25)


RUNNERS = {"pytest": step_pytest, "portable": step_portable, "dashboard": step_dashboard, "plugin": step_plugin,
           "e2e": step_e2e}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--e2e", action="store_true", help="also run scripts/e2e.py (about 18 minutes; commit first)")
    ap.add_argument("--skip", action="append", default=[], choices=STEPS[:-1], metavar="NAME",
                    help="leave a step out: " + ", ".join(STEPS[:-1]))
    args = ap.parse_args(argv)
    steps = [s for s in STEPS if s not in args.skip and (s != "e2e" or args.e2e)]
    results: list[tuple[str, str]] = []
    t_all = time.monotonic()
    for name in steps:
        print(f"… {name}", flush=True)
        t0 = time.monotonic()
        status, note, detail = RUNNERS[name]()
        print(f"{status}  {name}  ({time.monotonic() - t0:.0f}s)  {note}", flush=True)
        if detail:
            print(detail, flush=True)
        results.append((name, status))
    failed = [n for n, s in results if s == FAIL]
    skipped = [n for n, s in results if s == SKIP]
    print(f"\n{len(results) - len(failed) - len(skipped)} passed, {len(failed)} failed, {len(skipped)} skipped"
          f"  ({time.monotonic() - t_all:.0f}s)" + (f"; failed: {', '.join(failed)}" if failed else "")
          + (f"; skipped (not a pass): {', '.join(skipped)}" if skipped else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
