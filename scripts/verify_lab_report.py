#!/usr/bin/env python3
"""Check a lab's maintainer reports against the public repo, with `gh api` (optional).

    scripts/verify_lab_report.py REPORTS.json            # the JSON of GET /labs/SLUG/reports, saved
    scripts/verify_lab_report.py --relay URL SLUG        # fetched from a relay (reports are public)

For each report: a `merged` pull request exists, is merged and (when the report names a commit) was
merged as that commit; a `closed` / `fixed` / `confirmed` ref exists (and `closed`/`fixed` is closed).
Only `gh api` is called, read-only, with whatever login `gh` already has; Emporia holds no token and the
relay never does this. Without `gh` on PATH it says so and exits 0 (nothing was checked).
Exit 1 when a report does not match the repo, 0 otherwise.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.request


def gh_api(path: str) -> tuple[dict | None, str]:
    """(the JSON `gh api PATH` printed, "") or (None, why)."""
    p = subprocess.run([shutil.which("gh") or "gh", "api", path], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        return None, (p.stderr or p.stdout).strip().splitlines()[-1:][0] if (p.stderr or p.stdout).strip() else "failed"
    try:
        return json.loads(p.stdout), ""
    except ValueError:
        return None, "not JSON"


def check(report: dict) -> str:
    """'' when the report matches the repo, else what differs."""
    kind, _, ident = str(report.get("ref") or "").partition(":")
    repo, _, number = ident.partition("#")
    event, commit = report.get("event"), report.get("commit") or ""
    if kind == "gh_pr":
        d, why = gh_api(f"repos/{repo}/pulls/{number}")
        if d is None:
            return f"the pull request cannot be read ({why})"
        if event == "merged":
            if not d.get("merged"):
                return "the pull request is not merged"
            if commit and d.get("merge_commit_sha") != commit:
                return f"merged as {str(d.get('merge_commit_sha'))[:12]}, not the reported {commit[:12]}"
        elif event == "closed" and (d.get("state") != "closed" or d.get("merged")):
            return "the pull request is not closed unmerged"
        return ""
    if kind == "gh_issue":
        d, why = gh_api(f"repos/{repo}/issues/{number}")
        if d is None:
            return f"the issue cannot be read ({why})"
        if event in ("closed", "fixed") and d.get("state") != "closed":
            return "the issue is not closed"
        return ""
    return "not a gh_pr or gh_issue ref"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("source", help="a saved reports JSON file, or with --relay, the lab's slug")
    ap.add_argument("--relay", default="", help="a relay's base URL: fetch /labs/SLUG/reports from it")
    args = ap.parse_args(argv)
    if not shutil.which("gh"):
        print("gh is not on PATH: nothing was checked (the reports are public; check them by hand)")
        return 0
    if args.relay:
        url = f"{args.relay.rstrip('/')}/labs/{args.source}/reports?limit=200"
        with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 — the operator's own relay URL
            data = json.load(r)
    else:
        with open(args.source) as f:
            data = json.load(f)
    reports = data.get("reports", data) if isinstance(data, dict) else data
    bad = 0
    for r in reports:
        why = check(r)
        print(f"{'OK      ' if not why else 'MISMATCH'} {r.get('ref')} {r.get('event')}"
              + (f": {why}" if why else ""))
        bad += bool(why)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
