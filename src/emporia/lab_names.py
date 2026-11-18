"""How an agent names a lab: `owner/repo`, like GitHub (the lab's slug is the relay's own alias).

A lab about a GitHub repo is called `owner/repo` everywhere an agent sees or passes one. The tools also take the
repo's URL or the slug and resolve all three to the same lab. A lab that is not about a repo keeps its slug as its
name. The same goes for the PR or issue a thread hangs from: `gh_pr:owner/repo#N` is the stored form; a GitHub URL
(`.../pull/N`, `.../issues/N`) or a bare `owner/repo#N` is accepted too. Pure: no I/O (the tools bring the lists).
"""

from __future__ import annotations

import re
from typing import Any

_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_URL = re.compile(r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100}?)(?:\.git)?"
                  r"(?:/(pull|issues)/([1-9][0-9]{0,8}))?/?(?:[?#].*)?$", re.IGNORECASE)
_BARE_GH = re.compile(r"^([A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100})#([1-9][0-9]{0,8})$")


def repo_of(value: Any) -> str:
    """`owner/repo` from `owner/repo`, a GitHub repo URL (with `.git`, or a PR or issue path) or ''. Lowercase."""
    text = str(value or "").strip()
    if m := _URL.match(text):
        return f"{m.group(1)}/{m.group(2)}".lower()
    return text.lower() if _REPO.fullmatch(text) else ""


def name_of(lab: dict[str, Any]) -> str:
    """What an agent calls this lab: `owner/repo` for a repo's lab, else the slug."""
    subject = lab.get("subject") or {}
    ref = str(subject.get("ref") or "")
    if ref and _REPO.fullmatch(ref):
        return ref
    own = repo_of(lab.get("repo_url"))
    return own or str(lab.get("slug") or "")


def named(lab: dict[str, Any]) -> dict[str, Any]:
    """The lab with `lab_name` (owner/repo or slug) first and the slug kept as its own field."""
    return {"lab_name": name_of(lab), **lab}


def listing(labs: list[dict[str, Any]]) -> str:
    """`slug (owner/repo), …`, for an error that says what is here."""
    out = []
    for lab in labs:
        n = name_of(lab)
        out.append(lab.get("slug", "") if n == lab.get("slug") else f"{lab.get('slug', '')} ({n})")
    return ", ".join(out) or "none"


def resolve(value: str, labs: list[dict[str, Any]]) -> tuple[str, str]:
    """(slug, "") for a lab named by slug, `owner/repo` or repo URL; ("", why) when none matches, with the labs here."""
    text = str(value or "").strip()
    for lab in labs:
        if lab.get("slug") == text:
            return text, ""
    repo = repo_of(text)
    if repo:
        for lab in labs:
            if repo in (repo_of((lab.get("subject") or {}).get("ref")), repo_of(lab.get("repo_url"))):
                return str(lab["slug"]), ""
    return "", (f"no lab {text!r} on this relay. Labs here: {listing(labs)}. "
                "Pass the lab as owner/repo (for example wujitsana/emporia)")


def gh_ref(value: str, event: str = "") -> tuple[str, str]:
    """A PR or issue named as a ref text `gh_pr:owner/repo#N` / `gh_issue:...`, from a ref that already has its
    kind, a GitHub URL, or a bare `owner/repo#N` (its kind from the report `event` when that settles it:
    merged is a PR; confirmed and fixed are an issue). Returns (ref, "") or ("", why). A text it does not
    recognise comes back unchanged for the usual ref check to judge."""
    text = str(value or "").strip()
    if m := _URL.match(text):
        if not m.group(3):
            return "", "that URL names a repo, not a pull request or issue: use .../pull/N or .../issues/N"
        kind = "gh_pr" if m.group(3).lower() == "pull" else "gh_issue"
        return f"{kind}:{m.group(1)}/{m.group(2)}#{m.group(4)}", ""
    if m := _BARE_GH.match(text):
        kind = {"merged": "gh_pr", "confirmed": "gh_issue", "fixed": "gh_issue"}.get(event, "")
        if not kind:
            return "", (f"name the kind: gh_pr:{text} or gh_issue:{text} (a PR or an issue; "
                        "the report's event does not say which)")
        return f"{kind}:{text}", ""
    return text, ""


def kind_in_threads(bare: str, threads: list[dict[str, Any]]) -> str:
    """The ref kind of `owner/repo#N` among a lab's thread refs: `gh_pr` or `gh_issue` when exactly one is there."""
    kinds = {t["ref"].partition(":")[0] for t in threads
             if str(t.get("ref", "")).partition(":")[2].lower() == bare.lower()
             and str(t.get("ref", "")).partition(":")[0] in ("gh_pr", "gh_issue")}
    return next(iter(kinds)) if len(kinds) == 1 else ""
