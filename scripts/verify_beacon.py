#!/usr/bin/env python3
"""Check a game's randomness against drand itself, not against the relay that ran it.

    scripts/verify_beacon.py SESSION_ID --relay URL [--offline]

A game that draws from the drand beacon (Bargain, the common-value auction, Liar's Dice) names a beacon round that
did not exist when each player signed its commitment. This reads the game from the relay, then asks drand's own
servers for that round (this needs the network; `--offline` skips it and checks only the relay's stored copy) and
checks: every commitment names a round far enough ahead of its signing time; the round is the latest one named; the
round's BLS signature is valid under the pinned drand chain key (verified here, in pure Python); the relay stored
exactly the signature drand serves; and the round's randomness (the hash of the signature) is printed.

Exit status: 0 when every check holds, 1 when one fails, 2 when the game cannot be read or has not drawn yet.
Needs `py_ecc` (a dependency of Emporia).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("session_id")
    ap.add_argument("--relay", required=True, help="the relay's URL")
    ap.add_argument("--offline", action="store_true", help="do not ask drand: check only the relay's stored copy")
    args = ap.parse_args(argv)
    from emporia import beacon, rand
    try:
        session = get_json(f"{args.relay.rstrip('/')}/sessions/{args.session_id}")
    except (urllib.error.URLError, ValueError, OSError) as e:
        print(f"cannot read the game from {args.relay}: {e}", file=sys.stderr)
        return 2
    data = (session.get("state") or {}).get("data") or {}
    r = data.get("rand") or (session.get("config") or {}).get("rand")
    if not isinstance(r, dict) or not r.get("commits"):
        print(f"{args.session_id} ({session.get('module_type')}) does not draw from the beacon", file=sys.stderr)
        return 2
    commits, rnd, sig = r["commits"], r.get("round"), r.get("beacon_sig")
    print(f"game {args.session_id} ({session.get('module_type')}): {len(commits)} commitments, round {rnd}")
    failed = 0

    def report(ok: bool, text: str) -> None:
        nonlocal failed
        failed += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {text}")

    for who, c in commits.items():
        why = rand.commit_round_problem(c.get("at", ""), c.get("beacon_round"))
        report(not why, f"{who}'s commitment, signed {c.get('at', '')[:19]}, names round {c.get('beacon_round')}"
                        + (f": {why}" if why else " (at least 10 rounds, 30 s, after it was signed)"))
    latest = max(int(c["beacon_round"]) for c in commits.values())
    report(rnd == latest, f"the game's round is the latest one a commitment names ({latest})")
    if not sig:
        print("  the round was not needed yet: no randomness has been drawn")
        return 2 if failed == 0 else 1
    report(beacon.verify(int(rnd), sig), f"the stored signature of round {rnd} is valid under drand's pinned key")
    if args.offline:
        print("  (offline: drand itself was not asked)")
    else:
        try:
            served = beacon.fetch(int(rnd), beacon.http_get)
        except Exception as e:  # noqa: BLE001
            served = None
            print(f"  could not reach drand: {e}", file=sys.stderr)
        if served is None:
            report(False, f"drand did not serve a valid round {rnd} (network, or the round is not out yet)")
        else:
            report(served == sig, f"the relay stored exactly the signature drand serves for round {rnd}")
    print(f"  randomness of round {rnd}: {beacon.randomness(sig)}")
    print(("all checks hold" if not failed else f"{failed} check(s) failed"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
