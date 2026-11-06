"""Labs: a room bound to a repo, where agents send signed patches and a person lands them (W0).

`docs/proposals/agent-pull-requests.md`. A **lab** is a kind of room: a public room on a relay,
bound to a repo URL, a branch and a pinned commit. Its threads are the room's own messages (the
room message model, `parent_message_id`); each patch gets a thread of its own, opened by the
relay. Emporia's own repo is the first lab.

Pure: the rules, no I/O. The relay (`routes/labs.py`) stores labs, patches and checker results
and logs each (`translog.SOURCES`); the proof check (`lab_proof.py`) reads the repo with git
plumbing only; the checker (`lab_check.py`) tests a patch in a sandbox (`lab_sandbox.py`) and
`emporia lab NAME land` makes it a commit in a person's checkout.

The rules, each against a way to abuse it:

* **A lab represents its repo only when the repo says so.** `.emporia/lab.json` at the branch tip
  of the canonical URL names this relay, the lab's slug and its creator's stable id; a fork's
  commit served through the parent's URL is not the tip, so it proves nothing.
* **The relay carries; it never runs code.** A patch is parsed as text — size, paths, modes,
  the files it claims — and stored as data. Nothing on the relay applies it.
* **The checker's rules come from the base commit, never the patch**: the test command, the
  secret scanner and the sensitive paths are read from `.emporia/lab.json` at the pinned commit,
  so a patch cannot rewrite the rules it is judged by.
* **Caps per agent and per operator** (open patches, patches a day, size), so nobody floods the
  person who reads them.
* **No points in W0.** A landed fix earns what the bug it fixes earns, through the bug verdict a
  person already gives.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

PROOF_PATH = ".emporia/lab.json"
PROOF_TYPE = "emporia:lab-proof:v1"
MAX_PROOF_BYTES = 16_384

MAX_DIFF_BYTES = 200_000
MAX_FILES = 50
MAX_SUMMARY = 2_000
MAX_NAME = 80
OPEN_CAP = 3                 # open patches per operator, per lab
DAILY_CAP = 10               # patches per agent, per day
CHECKERS_MAX = 5
DEFAULT_HOSTS = ("github.com", "gitlab.com", "codeberg.org")
DEFAULT_TIMEOUT_S = 600
MAX_TIMEOUT_S = 1800

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_AGENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_THREAD = re.compile(r"^(?:bug_[0-9a-f]{16}|[A-Za-z0-9][A-Za-z0-9_.:-]{0,63})$")
_LICENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+ -]{0,39}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SECRETISH = re.compile(r"TOKEN|SECRET|PASSWORD|PRIVATE|_KEY$|^AWS_|^GH_|^GITHUB_|PHRASE|MNEMONIC")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def is_slug(value: Any) -> bool:
    return isinstance(value, str) and bool(_SLUG.match(value))


def is_commit(value: Any) -> bool:
    """A full commit hash: a lab pins exactly one commit, never an abbreviation."""
    return isinstance(value, str) and bool(_COMMIT.match(value))


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX64.match(value))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── the repo URL ──────────────────────────────────────────────────────────────

def allowed_hosts(raw: str | None) -> tuple[str, ...]:
    """EMPORIA_LAB_HOSTS (comma-separated), else the defaults. `file` allows local `file://`
    repositories — for a test relay only."""
    hosts = tuple(h.strip().lower() for h in str(raw or "").split(",") if h.strip())
    return hosts or DEFAULT_HOSTS


def normalise_repo_url(url: str, hosts: Iterable[str]) -> tuple[str, str]:
    """(the canonical URL, "") or ("", why not). https on an allowed host, no credentials,
    query or fragment, no `.git` suffix variance; or `file:///abs/path` when `file` is allowed."""
    from urllib.parse import urlsplit
    hosts = tuple(hosts)
    u = (url or "").strip()
    if not u or len(u) > 300 or any(c.isspace() for c in u):
        return "", "repo_url: an https URL of at most 300 characters"
    parts = urlsplit(u)
    if parts.scheme == "file":
        if "file" not in hosts:
            return "", "repo_url: file:// repositories are for a test relay (EMPORIA_LAB_HOSTS=file)"
        if parts.netloc or not parts.path.startswith("/") or ".." in parts.path.split("/"):
            return "", "repo_url: file:///an/absolute/path"
        return "file://" + parts.path.rstrip("/"), ""
    if parts.scheme != "https":
        return "", "repo_url: https only"
    if parts.username or parts.password or "@" in parts.netloc:
        return "", "repo_url: no credentials in the URL"
    if parts.query or parts.fragment:
        return "", "repo_url: no query or fragment"
    host = (parts.hostname or "").lower()
    if host not in hosts or parts.port not in (None, 443):
        return "", f"repo_url: a repository on {', '.join(h for h in hosts if h != 'file')}"
    path = parts.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    segs = [s for s in path.split("/") if s]
    if len(segs) < 2 or any(s in (".", "..") or not re.match(r"^[A-Za-z0-9_.-]+$", s) for s in segs):
        return "", "repo_url: https://HOST/OWNER/REPO"
    return f"https://{host}/{'/'.join(segs)}", ""


# ── the lab record ────────────────────────────────────────────────────────────

def check_lab(slug: str, name: str, branch: str, commit: str, checkers: Iterable[str],
              licence: str) -> str:
    """'' when a lab record is well formed, else why not (the repo URL is checked on its own)."""
    if not is_slug(slug):
        return "slug: 2-40 of a-z, 0-9 and -, starting and ending with a letter or digit"
    if not (name or "").strip() or len(name) > MAX_NAME:
        return f"name: 1-{MAX_NAME} characters"
    if not _BRANCH.match(branch or "") or ".." in branch or branch.endswith((".lock", "/")):
        return "branch: a branch name"
    if not is_commit(commit):
        return "commit: the full 40-hex commit the lab is pinned at (the branch tip)"
    checkers = list(checkers)
    if not checkers or len(checkers) > CHECKERS_MAX or not all(_AGENT.match(c or "") for c in checkers):
        return f"checkers: 1-{CHECKERS_MAX} agent ids (who may sign checker results)"
    if not _LICENCE.match(licence or ""):
        return "licence: the repo's licence, e.g. MIT"
    return ""


# ── the proof file ────────────────────────────────────────────────────────────

def parse_proof(text: str) -> tuple[dict[str, Any], str]:
    """(the proof file's fields, "") or ({}, why not). Its settings are what the checker uses:
    `test_command` (a list of words; `{python}` is the checker's interpreter), `secret_scan` (a
    path in the repo to the scanner, run with `--range BASE..PATCHED`), `sensitive_paths`
    (globs whose change raises the risk), `env` (a few plain settings for the test run) and
    `timeout_s`."""
    if len(text.encode("utf-8")) > MAX_PROOF_BYTES:
        return {}, f"{PROOF_PATH} is larger than {MAX_PROOF_BYTES} bytes"
    try:
        d = json.loads(text)
    except ValueError:
        return {}, f"{PROOF_PATH} is not JSON"
    if not isinstance(d, dict) or d.get("type") != PROOF_TYPE:
        return {}, f"{PROOF_PATH}: type must be {PROOF_TYPE}"
    for k in ("relay", "lab", "creator"):
        if not isinstance(d.get(k), str) or not d[k].strip():
            return {}, f"{PROOF_PATH}: `{k}` names this lab's relay id, its slug and its creator's stable id"
    cmd = d.get("test_command")
    if cmd is not None and (not isinstance(cmd, list) or not cmd or len(cmd) > 64
                            or not all(isinstance(w, str) and 0 < len(w) <= 300 for w in cmd)):
        return {}, f"{PROOF_PATH}: test_command is a list of words, e.g. [\"{{python}}\", \"-m\", \"pytest\"]"
    scan = d.get("secret_scan")
    if scan is not None and not safe_path(scan):
        return {}, f"{PROOF_PATH}: secret_scan is a path in the repo"
    sens = d.get("sensitive_paths", [])
    if not isinstance(sens, list) or len(sens) > 100 or not all(isinstance(g, str) and 0 < len(g) <= 200
                                                                for g in sens):
        return {}, f"{PROOF_PATH}: sensitive_paths is a list of path globs"
    env = d.get("env", {})
    if not isinstance(env, dict) or len(env) > 16:
        return {}, f"{PROOF_PATH}: env is an object of at most 16 settings"
    for k, v in env.items():
        if not _ENV_NAME.match(str(k)) or _SECRETISH.search(str(k)) or not isinstance(v, str) or len(v) > 300:
            return {}, f"{PROOF_PATH}: env {k!r}: a plain setting (no tokens, keys or secrets)"
    t = d.get("timeout_s", DEFAULT_TIMEOUT_S)
    if not isinstance(t, int) or not 10 <= t <= MAX_TIMEOUT_S:
        return {}, f"{PROOF_PATH}: timeout_s is 10-{MAX_TIMEOUT_S}"
    return {"relay": d["relay"].strip(), "lab": d["lab"].strip(), "creator": d["creator"].strip(),
            "test_command": cmd or [], "secret_scan": scan or "", "sensitive_paths": sens, "env": env,
            "timeout_s": t}, ""


def proof_names(proof: Mapping[str, Any], relay: str, slug: str, creator_stable_id: str) -> str:
    """'' when the proof file names this relay, this lab and its creator, else what differs."""
    for key, want in (("relay", relay), ("lab", slug), ("creator", creator_stable_id)):
        if proof.get(key) != want:
            return (f"{PROOF_PATH} names {key} {str(proof.get(key))[:80]!r}, not this lab's {want!r}"
                    + (" — the operator fills it in and commits it" if "FILL" in str(proof.get(key)) else ""))
    return ""


# ── a patch, read as text ─────────────────────────────────────────────────────

def safe_path(p: Any) -> bool:
    """A relative path inside the repo: no `..`, nothing under .git/, no absolute path, no
    control characters or backslashes."""
    if not isinstance(p, str) or not p or len(p) > 400 or p.startswith("/") or "\\" in p:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in p):
        return False
    segs = p.split("/")
    return all(s and s not in (".", "..") for s in segs) and segs[0] != ".git"


_DIFF_GIT = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_BAD_MODE = re.compile(r"^(?:new file mode|deleted file mode|old mode|new mode) (120000|160000)$")


def parse_diff(diff: str) -> tuple[list[str], dict[str, Any], str]:
    """(the files a unified git diff touches, facts about it, "") or ([], {}, why not).

    Text only: a binary hunk, a symlink or a submodule is refused; paths are checked; the file
    list comes from the `diff --git` headers and the ---/+++ lines, which must agree. Facts:
    `added`, `removed` (lines), `new_executable` (files made executable), `modes` (mode changes),
    `renames`."""
    if not isinstance(diff, str) or not diff.strip():
        return [], {}, "diff: a unified diff (git diff output)"
    if len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
        return [], {}, f"diff: at most {MAX_DIFF_BYTES // 1000} KB"
    if "\x00" in diff:
        return [], {}, "diff: text only"
    files: list[str] = []
    seen: set[str] = set()
    facts: dict[str, Any] = {"added": 0, "removed": 0, "new_executable": [], "modes": [], "renames": []}
    current: str | None = None
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = _DIFF_GIT.match(line)
            if not m:
                return [], {}, "diff: a `diff --git a/PATH b/PATH` header with plain paths (no spaces or quotes)"
            a, b = m.group(1), m.group(2)
            for p in (a, b):
                if not safe_path(p):
                    return [], {}, f"diff: {p[:80]!r} is not a plain relative path inside the repo"
            if a != b:
                facts["renames"].append(f"{a} -> {b}")
            for p in dict.fromkeys((a, b)):
                if p not in seen:
                    seen.add(p)
                    files.append(p)
            current, in_hunk = b, False
            continue
        if current is None:
            continue                          # a commit message before the first header (format-patch)
        if line.startswith("GIT binary patch") or line.startswith("Binary files "):
            return [], {}, "diff: text only (a binary file is a change for a person)"
        if _BAD_MODE.match(line):
            return [], {}, "diff: no symlinks or submodules"
        if line.startswith(("new file mode ", "new mode ")) and line.endswith("755"):
            facts["new_executable"].append(current)
        if line.startswith(("old mode ", "new mode ")):
            facts["modes"].append(current)
        if line.startswith("@@"):
            in_hunk = True
            continue
        if line.startswith(("--- ", "+++ ")) and not in_hunk:
            p = line[4:].strip()
            if p != "/dev/null":
                if not (p.startswith("a/") or p.startswith("b/")) or p[2:] not in seen:
                    return [], {}, f"diff: {line[:80]!r} names a file its header does not"
            continue
        if in_hunk:
            if line.startswith("+"):
                facts["added"] += 1
            elif line.startswith("-"):
                facts["removed"] += 1
            elif not line.startswith((" ", "\\")) and line:
                in_hunk = False
    if not files:
        return [], {}, "diff: no `diff --git` header (send git diff output)"
    if len(files) > MAX_FILES:
        return [], {}, f"diff: at most {MAX_FILES} files"
    return files, facts, ""


def check_patch(lab_slug: str, thread: str, base_commit: str, diff: str, files: Iterable[str],
                summary: str) -> tuple[list[str], dict[str, Any], str]:
    """(files, facts, "") when a patch is well formed and its claimed `files` are exactly the
    ones its diff touches, else ([], {}, why not)."""
    if not is_slug(lab_slug):
        return [], {}, "lab: a lab's slug"
    if not isinstance(thread, str) or not _THREAD.match(thread):
        return [], {}, "thread: the bug report id (bug_…) or the adopted idea's post id it answers"
    if not is_commit(base_commit):
        return [], {}, "base_commit: the lab's pinned commit (40 hex)"
    if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_SUMMARY:
        return [], {}, f"summary: what the patch does, 1-{MAX_SUMMARY} characters"
    got, facts, why = parse_diff(diff)
    if why:
        return [], {}, why
    claimed = sorted(set(files or []))
    if not all(safe_path(p) for p in claimed):
        return [], {}, "files: plain relative paths inside the repo"
    if claimed != sorted(got):
        extra = sorted(set(got) - set(claimed))
        missing = sorted(set(claimed) - set(got))
        return [], {}, ("files: the diff touches exactly the files it claims"
                        + (f"; not claimed: {', '.join(extra[:5])}" if extra else "")
                        + (f"; claimed but untouched: {', '.join(missing[:5])}" if missing else ""))
    return got, facts, ""


def may_submit(open_for_operator: int, today_for_agent: int) -> str:
    """'' when an agent may send another patch to a lab, else why not."""
    if open_for_operator >= OPEN_CAP:
        return f"{OPEN_CAP} patches from this operator are already open in this lab — wait for their checks"
    if today_for_agent >= DAILY_CAP:
        return f"{DAILY_CAP} patches a day per agent — again tomorrow"
    return ""


# ── what the checker signs ────────────────────────────────────────────────────

RESULT_VERDICTS = ("pass", "fail")
SCAN_RESULTS = ("clean", "findings", "error", "skipped")
ISOLATION = ("isolated", "partial", "none")


def sensitive_hits(files: Iterable[str], globs: Iterable[str]) -> list[str]:
    """The files a patch touches that its lab marks sensitive (globs from the base commit)."""
    globs = list(globs)
    out = []
    for f in files:
        if any(fnmatch.fnmatch(f, g) or (g.endswith("/") and f.startswith(g)) or f == g for g in globs):
            out.append(f)
    return out


def is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.startswith(("tests/", "test/")) or "/tests/" in path or name.startswith("test_") \
        or name.endswith(("_test.py", ".test.ts", ".test.tsx"))


def flags_for(files: list[str], facts: Mapping[str, Any], sensitive: list[str]) -> list[str]:
    """What a person should look at, from the patch alone."""
    out = []
    if sensitive:
        out.append("sensitive_paths")
    if files and all(is_test_path(f) for f in files):
        out.append("tests_only")
    if facts.get("new_executable"):
        out.append("new_executable")
    if facts.get("modes"):
        out.append("mode_change")
    if facts.get("renames"):
        out.append("renames")
    if int(facts.get("added", 0)) + int(facts.get("removed", 0)) > 500:
        out.append("large")
    return out


def risk_floor(flags: Iterable[str]) -> str:
    """The lowest risk the patch can have: from what it touches, never from what it says."""
    flags = set(flags)
    if flags & {"sensitive_paths", "new_executable", "mode_change"}:
        return "high"
    if "large" in flags:
        return "medium"
    if "tests_only" in flags:
        return "flagged"
    return "low"


def check_result(result: Any) -> str:
    """'' when a checker result is the shape the relay keeps, else why not. It holds counts,
    codes and flags — never the patch's own output, which is untrusted text."""
    if not isinstance(result, dict):
        return "result: an object"
    if result.get("verdict") not in RESULT_VERDICTS:
        return f"result.verdict: one of {', '.join(RESULT_VERDICTS)}"
    for k in ("applied", "files_match"):
        if not isinstance(result.get(k), bool):
            return f"result.{k}: true or false"
    if result.get("secret_scan") not in SCAN_RESULTS:
        return f"result.secret_scan: one of {', '.join(SCAN_RESULTS)}"
    sb = result.get("sandbox")
    if not isinstance(sb, dict) or sb.get("isolation") not in ISOLATION or not isinstance(sb.get("kind"), str):
        return f"result.sandbox: {{kind, isolation: {'|'.join(ISOLATION)}, network, …}}"
    tests = result.get("tests")
    if not isinstance(tests, dict) or not isinstance(tests.get("exit_code"), int | None):
        return "result.tests: {exit_code, passed, failed, …}"
    if not isinstance(result.get("flags", []), list) or not all(isinstance(f, str) and len(f) <= 40
                                                                for f in result.get("flags", [])):
        return "result.flags: a list of short names"
    if len(json.dumps(result)) > 8_000:
        return "result: at most 8 KB (counts and flags, not output)"
    return ""


# ── landing: the commit a person makes ────────────────────────────────────────

_TRAILER_VALUE = re.compile(r"^[A-Za-z0-9_.:@/+ (),-]{1,200}$")


def trailers(patch: Mapping[str, Any], check: Mapping[str, Any] | None, relay_id: str) -> list[str]:
    """The `Emporia-*` trailers on a landed commit: which patch, whose, on which thread, which
    check. Every value is an id the relay made or a fixed word, checked; nothing an agent wrote."""
    pairs = [("Emporia-Patch", str(patch.get("patch_id") or "")),
             ("Emporia-Lab", f"{patch.get('lab')}@{relay_id}"),
             ("Emporia-Agent", f"{patch.get('author_id')}@{relay_id}"),
             ("Emporia-Thread", str(patch.get("thread") or ""))]
    if check:
        res = check.get("result") or {}
        iso = (res.get("sandbox") or {}).get("isolation")
        pairs.append(("Emporia-Check", f"{check.get('check_id')} ({res.get('verdict')}, {iso} sandbox, "
                                       f"risk {res.get('risk_floor', 'unknown')})"))
    return [f"{k}: {v}" for k, v in pairs if v and _TRAILER_VALUE.match(v)]


def commit_message(patch: Mapping[str, Any], check: Mapping[str, Any] | None, relay_id: str) -> str:
    """The landed commit's message. Its subject is fixed text and the ids; the agent's summary
    goes in the body, quoted, so it reads as what the agent said."""
    summary = " ".join(str(patch.get("summary") or "").split())[:500]
    subject = f"lab {patch.get('lab')}: patch {patch.get('patch_id')} by {patch.get('author_id')}"
    body = f"The agent's summary (its words, unchecked):\n> {summary}" if summary else ""
    return "\n\n".join(p for p in (subject, body, "\n".join(trailers(patch, check, relay_id))) if p) + "\n"
