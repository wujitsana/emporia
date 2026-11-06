#!/usr/bin/env python3
"""Re-check an Emporia open game export offline: no relay, no network.

    scripts/verify_export.py games.jsonl [--relay-key HEX] [--quiet]

For every line (`emporia:game-export:v1`, from `GET /games/export`) it checks, from the file alone:
the public log's hash chain; every move's Ed25519 signature against its player's key (and key log);
for games that drew from the beacon, each player's signed commitment, that every revealed secret
hashes to it, the drand round's BLS signature against the pinned chain key, and that the published
dice, signals and values recompute from the secrets and the beacon; that the rules, run on the final
state, give the exported result; and the relay's signed referee result. `--relay-key` pins the relay
key (otherwise the one in each line is used, and the finding says it is not pinned).

Exit status: 0 when every line verifies, 1 when any line fails, 2 for an unreadable file.
"""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file", help="a JSON-lines export ('-' for stdin)")
    ap.add_argument("--relay-key", default="", help="the relay's pinned signing key (hex)")
    ap.add_argument("--quiet", action="store_true", help="print failures and the summary only")
    args = ap.parse_args(argv)
    from emporia.game_export import verify_line
    try:
        text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
    except OSError as e:
        print(f"cannot read {args.file}: {e}", file=sys.stderr)
        return 2
    checked = failed = 0
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            line = json.loads(raw)
        except ValueError:
            print(f"line {n}: not JSON", file=sys.stderr)
            return 2
        report = verify_line(line, relay_key=args.relay_key or None)
        checked += 1
        bad = report["verdict"] != "ok"
        failed += bad
        if bad or not args.quiet:
            print(f"{'FAIL' if bad else 'ok  '} {report['session_id'] or '?'} ({line.get('module', '?')})"
                  + (f": {report['unchecked']} unchecked" if report["unchecked"] and not bad else ""))
        if bad:
            for f in report["findings"]:
                if f["status"] == "fail":
                    print(f"     - {f['check']}: {f['detail']}")
    print(f"{checked} game(s) checked, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
