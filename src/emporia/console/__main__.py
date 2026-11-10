"""Run the local console (P126).

    python -m emporia.console                      # standalone, 127.0.0.1:8089, prints a link
    python -m emporia.console --uds PATH --profile DIR   # for the Hermes dashboard plugin

Keys, standalone: every Hermes profile's key directory under --hermes-root
(found from this checkout by default), or the --keys-dir directories given.
On a socket (console scope): only the one profile --profile names — the one
whose dashboard plugin started it — and only with the per-start secret the
plugin writes on stdin (one line), which every request then carries. Relay:
--relay-url, else EMPORIA_RELAY_URL.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

DEFAULT_PORT = 8089


def _hermes_root() -> Path:
    """The Hermes root this Emporia belongs to (P295: its profiles, the root's own included)."""
    from emporia import hermes_layout
    here = hermes_layout.containing_profile(Path(__file__))
    return hermes_layout.root_of(here) if here else Path.home() / ".hermes"


def _ui_dir() -> Path:
    # The built console page ships inside the package (`npm --prefix dashboard run build:console` rebuilds it), so a
    # pip install serves it too; before this it sat in an ignored folder and a fresh install answered 404.
    return Path(__file__).resolve().parent / "ui"


def _exit_with_parent(every_s: float = 5.0) -> None:
    """P239: the dashboard's console lives as long as the dashboard that started
    it. It used to outlive a restart (it runs in its own session, and the
    dashboard's exit hook does not run when it is killed), keep answering on its
    socket, and serve the code of whenever it was first started: the room-opening
    route a dashboard update added answered 404 for a day."""
    import threading
    import time

    parent = os.getppid()

    def watch() -> None:
        while True:
            time.sleep(every_s)
            if os.getppid() != parent:
                os._exit(0)
    threading.Thread(target=watch, daemon=True, name="exit-with-parent").start()


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    from emporia import hermes_layout
    from emporia.console.agents import discover
    from emporia.console.app import Console, build_app

    p = argparse.ArgumentParser(description="Emporia local console")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--uds", help="serve on this Unix socket (Hermes dashboard plugin)")
    where.add_argument("--port", type=int,
                       default=int(os.getenv("EMPORIA_CONSOLE_PORT", DEFAULT_PORT)))
    p.add_argument("--relay-url", default=os.getenv("EMPORIA_RELAY_URL", "http://localhost:8088"))
    p.add_argument("--hermes-root", default="", help="the Hermes home whose profiles hold the agents")
    p.add_argument("--keys-dir", action="append", default=[])
    p.add_argument("--profile", default="", help="on a socket: the one Hermes profile it serves")
    args = p.parse_args(argv)

    if args.uds:
        # Console scope: one profile, one secret per start (see the module docstring).
        if not args.profile or args.hermes_root or args.keys_dir:
            print("error: a socket console serves one profile: --profile DIR, "
                  "without --hermes-root or --keys-dir", file=sys.stderr)
            return 1
        secret = sys.stdin.readline().strip()
        if len(secret) < 32:
            print("error: no socket secret on stdin (the dashboard plugin writes one at each start)",
                  file=sys.stderr)
            return 1
        key_dirs = [Path(args.profile).expanduser() / "home" / ".hermes" / "keys"]
    else:
        secret = None
        key_dirs = [Path(d).expanduser() for d in args.keys_dir] or hermes_layout.key_dirs(
            Path(args.hermes_root).expanduser() if args.hermes_root else _hermes_root())
    found = discover(key_dirs)
    if not found:
        print("error: no agent keys found; pass --keys-dir", file=sys.stderr)
        return 1

    if args.uds:
        sock = Path(args.uds)
        sock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(sock.parent, 0o700)
        if sock.exists():
            sock.unlink()
        console = Console(key_dirs, args.relay_url, session_token=None, socket_secret=secret)
        _exit_with_parent()
        print(f"Emporia console on {sock} for {', '.join(sorted(found))} → {args.relay_url}",
              file=sys.stderr)
        uvicorn.run(build_app(console), uds=str(sock), log_level="warning")
        return 0

    token = secrets.token_urlsafe(24)
    console = Console(key_dirs, args.relay_url, session_token=token)
    print(f"Emporia console for {', '.join(sorted(found))} → relay {args.relay_url}")
    print("Open (once; it sets a session cookie for this browser):")
    print(f"  http://127.0.0.1:{args.port}/session?key={token}")
    uvicorn.run(build_app(console, ui_dir=_ui_dir()), host="127.0.0.1", port=args.port,
                log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
