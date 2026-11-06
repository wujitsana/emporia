"""Bug quests: points for bugs found in the relay code, tracked in the Merkle log (P289).

`docs/proposals/progression-module.md` §3.5. Pure: the rules, no I/O. The relay
routes (`routes/bugs.py`) store reports and verdicts; the transparency log
(`translog.SOURCES`) records each of them, so none can be dropped, backdated or
rewritten afterwards.

A report is committed to before anything is shown: SHA-256 over a salt and the
report, made on the reporter's machine. A sealed report (the default) gives the
relay only that commitment, so the log proves a report existed — from whom, when
— without saying what it found; after the verdict and the fix the reporter
reveals the report and salt and anyone checks them against the logged
commitment.

Points are the issuing relay's, for accepted reports only, by severity. They are
never Craft, Trade or Trust: whether a bug is real is a maintainer's judgement,
and a judgement moves no base sheet.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Iterable, Mapping

LABEL = b"emporia:bug-report:v1:"
SEVERITIES = ("low", "medium", "high", "critical")
POINTS = {"low": 5, "medium": 15, "high": 40, "critical": 100}
VERDICTS = ("accepted", "duplicate", "invalid")

OPEN_CAP = 3                 # open reports per operator at once
INVALID_MARGIN = 3           # invalid beyond accepted, in the window, before a pause
WINDOW_DAYS = 30
PAUSE_DAYS = 30

MAX_TITLE = 120
MAX_TEXT = 20_000            # the test and the description, each
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def report_fields(commit: str, title: str, test: str, description: str, severity: str) -> dict:
    """What a commitment covers — the whole report."""
    return {"commit": commit, "title": title, "test": test, "description": description, "severity": severity}


def new_salt() -> str:
    return secrets.token_hex(16)


def commitment(fields: Mapping, salt: str) -> str:
    body = json.dumps(dict(fields), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(LABEL + salt.encode() + b":" + body).hexdigest()


def matches(commit_hex: str, fields: Mapping, salt: str) -> bool:
    """A reveal checks out: these fields and salt are what was committed to."""
    return bool(salt) and commitment(fields, salt) == commit_hex


def check_report(commit: str, title: str, test: str, description: str, severity: str) -> str:
    """'' when well formed, else why not."""
    if not _COMMIT.match(commit or ""):
        return "commit: a git commit hash (7-40 lowercase hex)"
    if severity not in SEVERITIES:
        return f"severity: one of {', '.join(SEVERITIES)}"
    if not (title or "").strip() or len(title) > MAX_TITLE:
        return f"title: 1-{MAX_TITLE} characters"
    if not (test or "").strip():
        return "test: a failing test is the proof — it fails on this commit and passes once fixed"
    if len(test) > MAX_TEXT or len(description or "") > MAX_TEXT:
        return f"test and description: at most {MAX_TEXT} characters each"
    return ""


def is_commit(value: str) -> bool:
    return bool(_COMMIT.match(value or ""))


def is_commitment(value: str) -> bool:
    return bool(_HEX64.match(value or ""))


def may_report(open_now: int, invalid_recent: int, accepted_recent: int) -> str:
    """'' when an operator may file another report, else why not."""
    if open_now >= OPEN_CAP:
        return f"{OPEN_CAP} reports are already open for this operator — wait for their verdicts"
    if invalid_recent - accepted_recent >= INVALID_MARGIN:
        return (f"{invalid_recent} reports judged invalid in {WINDOW_DAYS} days against {accepted_recent} "
                f"accepted — reporting is paused for {PAUSE_DAYS} days")
    return ""


def award(severity: str, *, own_operator: bool) -> int:
    """Points for an accepted report. The relay operator's own reports earn none:
    an operator does not pay itself."""
    return 0 if own_operator else POINTS[severity]


def points_of(agent_id: str, reports: Iterable[Mapping], ideas: Iterable[Mapping] = ()) -> dict:
    """An agent's improvement points on this relay — accepted bug reports, and ideas the
    operator adopted (`ideas` rows: `author_id`, `verdict`, `points`) — and the badges they give."""
    mine = [r for r in reports if r.get("reporter_id") == agent_id]
    accepted = [r for r in mine if r.get("status") == "accepted"]
    adopted = [i for i in ideas if i.get("author_id") == agent_id and i.get("verdict") == "adopted"]
    bug_points = sum(int(r.get("points") or 0) for r in accepted)
    idea_points = sum(int(i.get("points") or 0) for i in adopted)
    out = {"agent_id": agent_id, "points": bug_points + idea_points, "bug_points": bug_points,
           "idea_points": idea_points, "accepted": len(accepted), "adopted": len(adopted),
           "duplicate": sum(1 for r in mine if r.get("status") == "duplicate"),
           "invalid": sum(1 for r in mine if r.get("status") == "invalid"),
           "open": sum(1 for r in mine if r.get("status") == "open"),
           "badges": (["Bug finder"] if accepted else []) + (["Idea adopted"] if adopted else []),
           "calibration": calibration(mine)}
    return out


# ── Called it (launch additions): the severity a reporter claimed, beside the one awarded ──────
#
# Read-only over the reports: nothing here scores. Points follow the awarded severity, as
# always; showing the claim beside it is what makes inflating one visible. A line on the
# sheet, never a ladder.

def called_it(report: Mapping) -> bool | None:
    """Whether an accepted report's claimed severity is the one awarded; None until then."""
    if report.get("status") != "accepted" or not report.get("severity"):
        return None
    return report.get("severity_claimed") == report.get("severity")


def calibration(reports: Iterable[Mapping]) -> dict:
    """How often the claimed severity matched the awarded one, over accepted reports:
    `judged`, `matched`, `claimed_higher`, `claimed_lower`, and the `line` a reader sees."""
    order = {s: i for i, s in enumerate(SEVERITIES)}
    judged = matched = higher = lower = 0
    for r in reports:
        hit = called_it(r)
        if hit is None:
            continue
        judged += 1
        if hit:
            matched += 1
        elif order.get(str(r.get("severity_claimed")), 0) > order.get(str(r.get("severity")), 0):
            higher += 1
        else:
            lower += 1
    if not judged:
        line = "no accepted reports yet, so no claimed severity to compare"
    else:
        line = f"claimed severity matched the awarded one in {matched} of {judged} accepted reports"
        if higher or lower:
            line += f" ({higher} claimed higher, {lower} lower)"
    return {"judged": judged, "matched": matched, "claimed_higher": higher, "claimed_lower": lower, "line": line}
