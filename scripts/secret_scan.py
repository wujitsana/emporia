#!/usr/bin/env python3
"""Scan what a push would publish for secrets, and refuse the push if any turn up.

    python scripts/secret_scan.py                  # commits not yet on any remote branch
    python scripts/secret_scan.py --all            # every commit reachable from any ref
    python scripts/secret_scan.py --range A..B     # one range

As a git pre-push hook it reads git's "<local ref> <sha> <remote ref> <sha>" lines on
stdin and scans exactly the commits each push adds. Install: `python scripts/secret_scan.py
--install-hook`.

It looks at **file names** (auth.json, .env, key files, databases) and at every **added line**
(private keys, API tokens, 64-hex values given to key/secret/phrase/seed settings). It prints
where a finding is and what kind, never the value. A finding blocks the push; a line that is
truly a fixture can end with `# secret-scan: allow`.

What it cannot see: a secret with no recognisable shape (a bare recovery phrase in prose).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ZERO = "0" * 40
ALLOW = "secret-scan: allow"

FILE_PATTERNS = re.compile(
    r"(^|/)(auth\.json|\.env|\.env\.(?!example$)[^/]+|[^/]+\.(priv|pem|key|p12|pfx|sqlite3?|db)|"
    r"id_(rsa|ed25519|ecdsa)|secrets/[^/]+)$", re.IGNORECASE)

LINE_PATTERNS = {
    "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?!\\n[A-Za-z0-9+/]{0,8}\.\.\.)",
    "OpenAI-style key": r"\bsk-(proj-)?[A-Za-z0-9_-]{24,}",
    "Anthropic key": r"\bsk-ant-[A-Za-z0-9_-]{20,}",
    "xAI key": r"\bxai-[A-Za-z0-9]{30,}",
    "GitHub token": r"\b(ghp|gho|ghs|ghu)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}",
    "AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
    "Stripe key": r"\b(sk|rk)_(live|test)_[A-Za-z0-9]{20,}",
    "Slack token": r"\bxox[bpoa]-[A-Za-z0-9-]{20,}",
    "Google API key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "Telegram bot token": r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}",
    "hex key in a setting": r"(PRIVATE|SECRET|PHRASE|SEED|MNEMONIC)[A-Z_]*\s*[=:]\s*['\"]?(0x)?[0-9a-fA-F]{64}\b",
}
_LINE = {k: re.compile(p) for k, p in LINE_PATTERNS.items()}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def scan(rev_args: list[str]) -> list[tuple[str, str, str]]:
    """(commit, path, kind) for each finding in the commits `rev_args` selects."""
    found: list[tuple[str, str, str]] = []
    out = _git("log", *rev_args, "-p", "--no-color", "--no-ext-diff", "--format=@@COMMIT %h")
    commit = path = ""
    for line in out.splitlines():
        if line.startswith("@@COMMIT "):
            commit = line.split()[1]
        elif line.startswith("diff --git a/") and " b/" in line:
            # Every changed file has this header; `+++ b/` only text changes do — a binary
            # file (a database, a .p12) or an empty one has none (review pass, 11-05).
            path = line.rsplit(" b/", 1)[1]
            if FILE_PATTERNS.search(path):
                found.append((commit, path, "secret-like file"))
        elif line.startswith("deleted file mode") and found and found[-1] == (commit, path, "secret-like file"):
            found.pop()                    # removing a secret file is the remedy, not a finding
        elif line.startswith("+++ b/"):
            path = line[6:]
            if FILE_PATTERNS.search(path):
                found.append((commit, path, "secret-like file"))
        elif line.startswith("+") and not line.startswith("+++") and ALLOW not in line:
            for kind, rx in _LINE.items():
                if rx.search(line):
                    found.append((commit, path, kind))
    return sorted(set(found))


def _pushed_revs(stdin: str) -> list[str]:
    """The rev-list arguments for what a pre-push adds: each new tip, minus what any remote has."""
    tips = [sha for line in stdin.splitlines() if len(parts := line.split()) == 4
            and (sha := parts[1]) != ZERO]
    return [*tips, "--not", "--remotes"] if tips else []


def install_hook() -> int:
    hooks = Path(_git("rev-parse", "--git-path", "hooks").strip())
    hook = hooks / "pre-push"
    if hook.exists() and "secret_scan.py" not in hook.read_text():
        print(f"{hook} exists and is not this scan; add a call to scripts/secret_scan.py --hook to it yourself")
        return 1
    hook.write_text('#!/bin/sh\n# Refuse a push that would publish a secret (scripts/secret_scan.py).\n'
                    'exec python3 "$(git rev-parse --show-toplevel)/scripts/secret_scan.py" --hook\n')
    hook.chmod(0o755)
    print(f"installed {hook}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--all", action="store_true", help="scan every commit reachable from any ref")
    p.add_argument("--range", help="scan this revision range (A..B)")
    p.add_argument("--hook", action="store_true", help="run as git's pre-push hook (reads stdin)")
    p.add_argument("--install-hook", action="store_true", help="install the pre-push hook here")
    a = p.parse_args(argv)
    if a.install_hook:
        return install_hook()
    if a.hook:
        revs = _pushed_revs(sys.stdin.read())
        if not revs:
            return 0
    elif a.all:
        revs = ["--all"]
    elif a.range:
        revs = [a.range]
    else:
        revs = ["--all", "--not", "--remotes"]
    found = scan(revs)
    for commit, path, kind in found:
        print(f"  {commit}  {path}: {kind}")
    if found:
        print(f"secret scan: {len(found)} finding(s) — not pushing. Remove the secret from history "
              f"(not just the latest commit), or mark a true fixture line with '# {ALLOW}'.")
        return 1
    print("secret scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
