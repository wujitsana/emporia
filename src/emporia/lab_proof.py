"""Labs v2: whether a repo says a lab is its own — read with git plumbing only.

`docs/proposals/agent-pull-requests.md` §2.3. A lab represents its repo when the file
`.emporia/lab.json` **at the tip of the branch on the canonical URL** names the relay, the lab and
the creator's stable id. Git hosts serve a fork's commits through the parent's URL, so "the file
exists at commit X" proves nothing; the tip of the named branch does.

The check, the same for the relay (at creation) and any reader (`emporia lab NAME verify`):

1. `git ls-remote URL refs/heads/BRANCH` — the tip;
2. the pinned commit is that tip;
3. a depth-1 fetch of that branch into a throwaway bare repository, and `git show
   COMMIT:.emporia/lab.json`, size-capped — no checkout, no hooks, no filters, nothing run.

Every git here runs with no system or user config, hooks pointed at /dev/null, no prompts, only
the https (or, on a test relay, file) protocol, and a timeout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from emporia import labs
import shutil as _shutil


def _search_path() -> str:
    """A PATH of the system's folders and the one git is in, never the person's whole PATH."""
    found = _shutil.which("git")
    return ":".join(dict.fromkeys([*([os.path.dirname(found)] if found else []), "/usr/local/bin", "/usr/bin",
                                   "/bin"]))

GIT_TIMEOUT_S = 60
MAX_FETCH_BYTES = 500 * 1024 * 1024
#: A fetch leaves out any file larger than this (the proof file is a few KB): a repo cannot make the relay
#: download its big blobs.
FETCH_BLOB_LIMIT = "--filter=blob:limit=1m"


class GitError(RuntimeError):
    pass


def git_env(home: str | Path) -> dict[str, str]:
    """A git that reads no config of the person's and asks nothing."""
    return {"PATH": _search_path(), "HOME": str(home), "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false", "GIT_LFS_SKIP_SMUDGE": "1"}


def git_flags(allow_file: bool) -> list[str]:
    flags = ["-c", "core.hooksPath=" + os.devnull, "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
             "-c", "core.fsmonitor=false", "-c", "credential.helper=", "-c", "advice.detachedHead=false",
             "-c", "init.defaultBranch=main", "-c", "transfer.fsckObjects=true",
             "-c", "http.followRedirects=false"]
    if allow_file:
        flags += ["-c", "protocol.file.allow=always"]
    return flags


def git(args: list[str], *, cwd: str | Path | None = None, home: str | Path, allow_file: bool = False,
        timeout: float = GIT_TIMEOUT_S, input_text: str | None = None, extra_env: Mapping[str, str] | None = None
        ) -> str:
    """Run git plumbing; its stdout, or GitError with the last line of what it said."""
    try:
        p = subprocess.run(["git", *git_flags(allow_file), *args], cwd=cwd, env={**git_env(home), **(extra_env or {})},
                           input=input_text, stdin=None if input_text is not None else subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {args[0]} took longer than {int(timeout)}s") from e
    except FileNotFoundError as e:
        raise GitError("git is not installed") from e
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-1:] or ["failed"]
        raise GitError(f"git {args[0]}: {tail[0][:200]}")
    return p.stdout


def tip(repo_url: str, branch: str, *, home: str | Path, allow_file: bool) -> str:
    out = git(["ls-remote", "--", repo_url, f"refs/heads/{branch}"], home=home, allow_file=allow_file)
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/heads/{branch}" and labs.is_commit(sha.strip()):
            return sha.strip()
    raise GitError(f"{repo_url} has no branch {branch!r}")


def _size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def fetch_tip(repo_url: str, branch: str, into: Path, *, home: str | Path, allow_file: bool) -> str:
    """A depth-1 fetch of the branch into a bare repository at `into`; the commit fetched."""
    into.mkdir(parents=True, exist_ok=True)
    git(["init", "--quiet", "--bare", str(into)], home=home, allow_file=allow_file)
    git(["fetch", "--quiet", "--depth=1", "--no-tags", FETCH_BLOB_LIMIT, "--", repo_url, f"refs/heads/{branch}"],
        cwd=into, home=home, allow_file=allow_file)
    if _size(into) > MAX_FETCH_BYTES:
        raise GitError(f"the fetch is larger than {MAX_FETCH_BYTES // (1024 * 1024)} MB")
    return git(["rev-parse", "FETCH_HEAD"], cwd=into, home=home, allow_file=allow_file).strip()


def read_file(bare: Path, commit: str, path: str, *, home: str | Path, max_bytes: int = labs.MAX_PROOF_BYTES) -> str:
    """One file at one commit, from a bare repository, size-capped before it is read."""
    if not labs.is_commit(commit) or not labs.safe_path(path):
        raise GitError("not a commit and a path")
    try:
        size = int(git(["cat-file", "-s", f"{commit}:{path}"], cwd=bare, home=home).strip())
    except (GitError, ValueError) as e:
        raise GitError(f"no {path} at {commit[:12]}") from e
    if size > max_bytes:
        raise GitError(f"{path} at {commit[:12]} is larger than {max_bytes} bytes")
    return git(["cat-file", "blob", f"{commit}:{path}"], cwd=bare, home=home)


def verify_in_repo(repo: Path, commit: str, branch_tip: str, *, relay: str, slug: str, creator: str,
                   maintainers: tuple[str, ...] = (), home: str | Path) -> dict[str, Any]:
    """The proof, read in a repository that holds the branch's history: the pin is the tip or an
    ancestor of it; the proof file at the pin names this lab; and it still does at the tip (else
    the lab has **lapsed**: its repo took the file back). {represents, lapsed, why, proof}."""
    out: dict[str, Any] = {"represents": False, "lapsed": False, "why": "", "proof": None}
    if branch_tip != commit:
        try:
            git(["merge-base", "--is-ancestor", commit, branch_tip], cwd=repo, home=home)
        except GitError:
            out["why"] = f"the pinned {commit[:12]} is not on the branch (its tip is {branch_tip[:12]})"
            return out
    try:
        proof, why = labs.parse_proof(read_file(repo, commit, labs.PROOF_PATH, home=home))
    except GitError as e:
        out["why"] = str(e)
        return out
    why = why or labs.proof_names(proof, relay, slug, creator, maintainers)
    if why:
        out["why"] = why
        return out
    out["proof"] = proof
    if branch_tip != commit:
        try:
            now, why = labs.parse_proof(read_file(repo, branch_tip, labs.PROOF_PATH, home=home))
            why = why or labs.proof_names(now, relay, slug, creator, maintainers)
        except GitError as e:
            why = str(e)
        if why:
            out.update(lapsed=True, why=f"lapsed: at the branch tip {branch_tip[:12]}, {why}")
            return out
    out["represents"] = True
    return out


def check(repo_url: str, branch: str, commit: str, *, relay: str, slug: str, creator: str,
          maintainers: tuple[str, ...] = (), allow_file: bool = False, workdir: str | Path | None = None, recheck: bool = False) -> dict[str, Any]:
    """Whether the repo says it is this lab: {represents, lapsed, why, tip, proof, checked_at}.

    At creation (the relay) the branch tip must be the pinned commit and the proof file there must
    name `relay`, `slug` and `creator` (a stable id): a depth-1 fetch. A reader's `recheck` also
    takes a pin the branch has since moved past — fetching the branch's history to see the pin is
    on it, and that the tip still names the lab. Never raises: a failure is `represents` False
    with the reason."""
    from datetime import UTC, datetime
    now = datetime.now(UTC).isoformat(timespec="seconds")
    out: dict[str, Any] = {"represents": False, "lapsed": False, "why": "", "tip": None, "proof": None,
                           "checked_at": now, "unreachable": False}
    tmp = Path(tempfile.mkdtemp(prefix="emporia-lab-proof-", dir=workdir))
    try:
        home = tmp / "home"
        home.mkdir()
        bare = tmp / "repo.git"
        try:
            try:
                t = tip(repo_url, branch, home=home, allow_file=allow_file)
            except GitError as e:      # not readable without credentials: a private repo, or a wrong URL
                out["unreachable"] = True
                raise e
            out["tip"] = t
            if t != commit and not recheck:
                out["why"] = (f"the tip of {branch} is {t[:12]}, not the pinned {commit[:12]} — "
                              "a lab is pinned at the branch tip")
                return out
            if t == commit:
                fetched = fetch_tip(repo_url, branch, bare, home=home, allow_file=allow_file)
            else:
                bare.mkdir()
                git(["init", "--quiet", "--bare", str(bare)], home=home, allow_file=allow_file)
                git(["fetch", "--quiet", "--no-tags", FETCH_BLOB_LIMIT, "--", repo_url, f"refs/heads/{branch}"], cwd=bare, home=home,
                    allow_file=allow_file, timeout=300)
                if _size(bare) > MAX_FETCH_BYTES:
                    raise GitError(f"the fetch is larger than {MAX_FETCH_BYTES // (1024 * 1024)} MB")
                fetched = git(["rev-parse", "FETCH_HEAD"], cwd=bare, home=home).strip()
            if fetched != t:
                out["why"] = f"the branch moved while it was read ({fetched[:12]}); try again"
                return out
        except GitError as e:
            out["why"] = str(e)
            return out
        out.update(verify_in_repo(bare, commit, t, relay=relay, slug=slug, creator=creator,
                                     maintainers=maintainers, home=home))
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
