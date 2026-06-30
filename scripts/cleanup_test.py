#!/usr/bin/env python3
"""Reset Emporia to a clean state after testing.

What this removes:
  - emporia.sqlite3 (all relay data: agents, sessions, listings, etc.)
  - Dashboard .env.local files (VITE_AGENT_ID baked values)

What this does NOT touch:
  - Agent Ed25519 keypairs (~/.hermes/keys/)
  - Profile config.yaml / .env files
  - Hermes profile directories themselves
  - The relay process (use --stop-relay to also kill it)

Usage:
    python scripts/cleanup_test.py              # dry run (shows what would change)
    python scripts/cleanup_test.py --yes        # clean DB + .env.locals
    python scripts/cleanup_test.py --yes --stop-relay
    python scripts/cleanup_test.py --yes --drop-profiles   # also delete demo profiles
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent  # emporia/
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _load_dotenv() -> None:
    """Walk up from this script to find and load the nearest .env file."""
    for parent in _HERE.parents:
        env_file = parent / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            break

_load_dotenv()

DB_PATH = Path(
    os.getenv("EMPORIA_DB_PATH", "~/.hermes/emporia.sqlite3")
).expanduser()

_profiles_base = Path("/opt/data/profiles")
DASHBOARD_ENVS = list({
    p.resolve()
    for p in [
        _ROOT / "dashboard" / ".env.local",
        *_profiles_base.glob("*/emporia/dashboard/.env.local"),
    ]
})

# Exactly the profiles seeded by bootstrap — only these are deleted by --drop-profiles
DEMO_PROFILES = ["alpha", "beta", "nemotron_strategist", "stripe_escrow_bot"]
PROFILES_BASE = _profiles_base


def _find_relay_pids() -> list[int]:
    try:
        out = subprocess.run(
            ["pgrep", "-f", "emporia.relay_server"],
            capture_output=True, text=True,
        )
        return [int(p) for p in out.stdout.split() if p.strip()]
    except Exception:
        return []


def _stop_relay(dry: bool) -> None:
    pids = _find_relay_pids()
    if not pids:
        print("  relay: not running")
        return
    for pid in pids:
        print(f"  relay: kill {pid}")
        if not dry:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    if not dry:
        time.sleep(1)


def _delete(path: Path, dry: bool, label: str = "") -> bool:
    if not path.exists():
        return False
    print(f"  remove: {label or path}")
    if not dry:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", "-y", action="store_true",
                    help="Actually delete (default is dry run)")
    ap.add_argument("--stop-relay", action="store_true",
                    help="Kill the relay process")
    ap.add_argument("--drop-profiles", action="store_true",
                    help="Delete demo agent profiles (alpha, beta, nemotron_strategist, stripe_escrow_bot)")
    args = ap.parse_args()

    dry = not args.yes
    if dry:
        print("DRY RUN — pass --yes to actually delete\n")
    else:
        print("Cleaning up Emporia test state…\n")

    changed = False

    # 1. Always stop relay before deleting DB — relay must restart to re-init schema
    _stop_relay(dry)

    # 2. DB
    changed |= _delete(DB_PATH, dry, f"DB: {DB_PATH}")

    # 3. Dashboard .env.local files
    for env_file in DASHBOARD_ENVS:
        changed |= _delete(env_file, dry, f".env.local: {env_file}")

    # 4. Extra relay stop if explicitly requested (no-op if already stopped above)
    if args.stop_relay:
        pass  # already handled

    # 4. Drop demo profiles
    if args.drop_profiles:
        print()
        for name in DEMO_PROFILES:
            prof = PROFILES_BASE / name
            if not prof.exists():
                print(f"  profile {name}: not found")
                continue
            if dry:
                print(f"  [dry] would delete profile '{name}'")
            else:
                print(f"  deleting profile '{name}'…")
                try:
                    result = subprocess.run(
                        ["hermes", "profile", "delete", name],
                        input=name, capture_output=True, text=True, timeout=15,
                    )
                    if result.returncode == 0:
                        print(f"  profile '{name}' deleted")
                    else:
                        print(f"  warning: {result.stderr.strip()[-200:]}")
                except Exception as e:
                    print(f"  warning: could not delete '{name}': {e}")

    print()
    if dry:
        print("Dry run complete. Pass --yes to apply.")
    elif not changed and not args.drop_profiles:
        print("Nothing to clean up.")
    else:
        print("Done.")
        print("Relay stopped — restart it before running bootstrap or seed.")


if __name__ == "__main__":
    main()
