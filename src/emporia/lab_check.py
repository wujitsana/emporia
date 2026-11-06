"""Labs (W0): the checker — code, no model — that tests a patch and says what it found.

`docs/proposals/agent-pull-requests.md` §5. A person runs it, or schedules it, as
`emporia lab NAME check [SLUG]`; the plugin registers no cron job. For each patch:

1. **A fresh clone at the pinned commit**, from the checker's own mirror of the lab's repo URL —
   never from the patch, and fetched before any untrusted code runs.
2. **The rules from the base commit**: `.emporia/lab.json` there gives the test command, the
   secret scanner and the sensitive paths. The scanner's file is copied out of the base commit
   before the patch is applied, so a patch cannot rewrite the rules it is judged by.
3. **Apply** the diff (`git apply`, hooks off) and commit it in the clone; the files it touches
   must be exactly the ones it claims.
4. **In the sandbox** (`lab_sandbox.run`: no network, no secrets, only its own folder), the base
   commit's secret scanner over the patch's commit, then the base commit's test command on the
   patched tree.
5. **The result**: applied, files as claimed, the scan, the tests' exit code and counts, the
   flags and the risk floor, and where it ran (`sandbox.isolation`: isolated, partial or none).
   It holds counts and codes, never the patch's own output (that stays in the local log).

The CLI signs the result with the checker agent's key and sends it to the relay. Nothing here
merges, pushes or decides: a person lands a patch (`emporia lab NAME land`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from emporia import lab_proof, lab_sandbox, labs

VERSION = "lab-check:w0.1"
_COUNTS = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)\b")
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
_UNITTEST_FAILED = re.compile(r"^FAILED \((?:failures=(\d+))?(?:, )?(?:errors=(\d+))?", re.M)


def default_root() -> Path:
    """Where the checker works: EMPORIA_LAB_WORKDIR, else ~/.cache/emporia-lab-check — the
    person's, outside every agent's profile (the key guard keeps agents' tools out of it)."""
    raw = os.environ.get("EMPORIA_LAB_WORKDIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".cache" / "emporia-lab-check"


def _private_dir(p: Path) -> Path:
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def mirror(repo_url: str, root: Path, *, allow_file: bool) -> Path:
    """The checker's own bare mirror of the lab's repo, brought up to date (before any untrusted
    code runs: this is the only network the checker uses)."""
    m = _private_dir(root / "mirrors") / (hashlib.sha256(repo_url.encode()).hexdigest()[:16] + ".git")
    home = _private_dir(root / "git-home")
    if (m / "HEAD").exists():
        lab_proof.git(["fetch", "--quiet", "--prune", "--no-tags", "--", repo_url, "+refs/heads/*:refs/heads/*"],
                      cwd=m, home=home, allow_file=allow_file, timeout=600)
    else:
        lab_proof.git(["clone", "--quiet", "--bare", "--no-tags", "--", repo_url, str(m)], home=home,
                      allow_file=allow_file, timeout=900)
    return m


def counts(output: str) -> dict[str, int | None]:
    """passed / failed from the test run's own summary (pytest's or unittest's), as numbers."""
    out: dict[str, int | None] = {"passed": None, "failed": None, "errors": None, "skipped": None}
    for n, word in _COUNTS.findall(output[-4000:]):
        key = "errors" if word.startswith("error") else word if word in out else None
        if key:
            out[key] = int(n)
    ran = _UNITTEST_RAN.findall(output)
    if ran and out["passed"] is None:
        failed = _UNITTEST_FAILED.findall(output)
        f = sum(int(a or 0) + int(b or 0) for a, b in failed)
        out["passed"], out["failed"] = int(ran[-1]) - f, f
    return out


def _words(command: list[str], python: str) -> list[str]:
    return [python if w == "{python}" else w for w in command]


def _env_for(proof_env: Mapping[str, str], clone: Path) -> dict[str, str]:
    """The lab's plain settings for the test run; a relative path value is taken inside the clone."""
    out = {}
    for k, v in proof_env.items():
        if k == "PYTHONPATH":
            v = os.pathsep.join(str((clone / p).resolve()) if not p.startswith("/") else p for p in v.split(os.pathsep))
        out[k] = v
    return out


def land_patch(checkout: Path, patch: Mapping[str, Any], check: Mapping[str, Any] | None, relay_id: str,
               *, author: str = "") -> dict[str, Any]:
    """A person's `emporia lab NAME land PATCH --into CHECKOUT`: the patch as one commit on a new
    branch `lab/<patch id>` at its base, with the `Emporia-*` trailers — made without touching the
    checkout's working tree, index or current branch (a temporary index, `git apply --cached`,
    `commit-tree`: no hook runs, nothing of the patch is executed). Nothing is pushed.
    {branch, commit} or GitError."""
    checkout = checkout.resolve()
    if not (checkout / ".git").exists():
        raise lab_proof.GitError(f"{checkout} is not a git checkout")
    base = str(patch["base_commit"])
    home = Path.home()

    def g(*args: str, env: Mapping[str, str] | None = None, stdin: str | None = None) -> str:
        return lab_proof.git(list(args), cwd=checkout, home=home, input_text=stdin, extra_env=env)
    try:
        g("cat-file", "-e", f"{base}^{{commit}}")
    except lab_proof.GitError as e:
        raise lab_proof.GitError(f"the base commit {base[:12]} is not in {checkout}: fetch it first") from e
    branch = f"lab/{patch['patch_id']}"
    try:
        g("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        raise lab_proof.GitError(f"{branch} is there already: the patch was landed here before")
    except lab_proof.GitError as e:
        if "already" in str(e):
            raise
    with tempfile.TemporaryDirectory(prefix="emporia-lab-land-") as tmp:
        index = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        g("read-tree", base, env=index)
        try:
            g("apply", "--cached", "--whitespace=nowarn", "-", env=index, stdin=str(patch["diff"]))
        except lab_proof.GitError as e:
            raise lab_proof.GitError(f"the patch does not apply at {base[:12]}: {e}") from e
        tree = g("write-tree", env=index).strip()
    who = author or "emporia-lab"
    ident = {"GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": f"{patch.get('author_id')}@emporia.invalid",
             "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": f"{patch.get('author_id')}@emporia.invalid"}
    if not author:
        try:      # the person's own name as committer, when their checkout has one
            name = lab_proof.git(["config", "user.name"], cwd=checkout, home=Path.home()).strip()
            email = lab_proof.git(["config", "user.email"], cwd=checkout, home=Path.home()).strip()
            if name and email:
                ident.update(GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
        except lab_proof.GitError:
            pass
    commit = g("commit-tree", tree, "-p", base, env=ident, stdin=labs.commit_message(patch, check, relay_id)).strip()
    g("update-ref", f"refs/heads/{branch}", commit, "0" * 40)
    return {"branch": branch, "commit": commit, "base": base}


def check_patch(lab: Mapping[str, Any], patch: Mapping[str, Any], *, root: Path | None = None,
                python: str | None = None, allow_file: bool = False, allow_unisolated: bool = False,
                sandbox_info: Mapping[str, Any] | None = None, keep: bool = False,
                log: Any = None) -> dict[str, Any]:
    """The result for one patch (unsigned). `lab` and `patch` are the relay's records; the diff
    is `patch["diff"]`, already checked against its signed hash by the caller."""
    t0 = time.monotonic()
    root = _private_dir(root or default_root())
    python = python or os.environ.get("EMPORIA_LAB_PYTHON", "").strip() or sys.executable
    info = dict(sandbox_info or lab_sandbox.probe())
    say = log or (lambda _msg: None)
    base = str(patch.get("base_commit") or "")
    result: dict[str, Any] = {"verdict": "fail", "applied": False, "files_match": False, "secret_scan": "skipped",
                              "tests": {"exit_code": None}, "flags": [], "risk_floor": "unknown",
                              "sandbox": lab_sandbox.describe(info.get("backend", "none"), info),
                              "checker": VERSION, "problem": ""}
    if base != lab.get("commit"):
        result["problem"] = "the patch is not against the lab's pinned commit"
        return result
    work = Path(tempfile.mkdtemp(prefix=f"{patch.get('patch_id')}-", dir=_private_dir(root / "runs")))
    os.chmod(work, 0o700)
    home = _private_dir(work / "git-home")
    try:
        m = mirror(str(lab["repo_url"]), root, allow_file=allow_file)
        # the reader's re-check, in the checker's own mirror: the repo still says it is this lab
        branch_tip = lab_proof.git(["rev-parse", f"refs/heads/{lab['branch']}"], cwd=m, home=home).strip()
        proof_now = lab_proof.verify_in_repo(m, base, branch_tip, relay=str(lab.get("relay_id") or ""),
                                             slug=str(lab["slug"]), creator=str(lab.get("creator_stable_id") or ""),
                                             home=home)
        result["represents"] = proof_now["represents"]
        if not proof_now["represents"]:
            result["problem"] = f"the repo does not say it is this lab: {proof_now['why'][:200]}"
            return result
        clone = work / "clone"
        lab_proof.git(["clone", "--quiet", "--no-checkout", "--no-hardlinks", "--", str(m), str(clone)],
                      home=home, allow_file=True, timeout=600)
        lab_proof.git(["checkout", "--quiet", "--detach", base], cwd=clone, home=home)
        head = lab_proof.git(["rev-parse", "HEAD"], cwd=clone, home=home).strip()
        if head != base:
            result["problem"] = f"the clone is at {head[:12]}, not the pinned {base[:12]}"
            return result
        # the rules, from the base commit, before the patch touches anything
        proof, why = labs.parse_proof(lab_proof.read_file(clone, base, labs.PROOF_PATH, home=home))
        if why:
            result["problem"] = f"the base commit's {labs.PROOF_PATH}: {why}"
            return result
        rules = _private_dir(work / "rules")
        scanner = None
        if proof["secret_scan"]:
            scanner = rules / "secret_scan.py"
            scanner.write_text(lab_proof.read_file(clone, base, proof["secret_scan"], home=home,
                                                   max_bytes=512_000))
        # apply, hooks off, as data
        diff_file = work / "patch.diff"
        diff_file.write_text(str(patch["diff"]))
        try:
            lab_proof.git(["apply", "--check", "--whitespace=nowarn", str(diff_file)], cwd=clone, home=home)
            lab_proof.git(["apply", "--whitespace=nowarn", str(diff_file)], cwd=clone, home=home)
        except lab_proof.GitError as e:
            result["problem"] = f"the patch does not apply at the pinned commit: {str(e)[:160]}"
            return result
        result["applied"] = True
        lab_proof.git(["add", "-A"], cwd=clone, home=home)
        lab_proof.git(["-c", "user.name=emporia-lab-check", "-c", "user.email=lab-check@localhost",
                       "-c", "commit.gpgsign=false", "commit", "--quiet", "--no-verify", "-m",
                       f"lab patch {patch.get('patch_id')}"], cwd=clone, home=home)
        touched = sorted(lab_proof.git(["diff", "--name-only", "--no-renames", base, "HEAD"], cwd=clone,
                                       home=home).split())
        _files, facts, _ = labs.parse_diff(str(patch["diff"]))
        result["files_match"] = touched == sorted(patch.get("files") or [])
        sensitive = labs.sensitive_hits(touched, proof["sensitive_paths"])
        result["flags"] = labs.flags_for(touched, facts, sensitive)
        if not result["files_match"]:
            result["flags"].append("files_differ")
        result["risk_floor"] = labs.risk_floor(result["flags"])
        result["sensitive"] = sensitive[:20]
        env = lab_sandbox.sandbox_env(work, python, _env_for(proof["env"], clone))
        # the secret scan: the base commit's scanner, over the patch's commit, in the sandbox
        if scanner:
            say("  secret scan (the base commit's scanner) in the sandbox")
            scan = lab_sandbox.run([python, str(scanner), "--range", f"{base}..HEAD"], cwd=clone, work=work,
                                   python=python, env=env, timeout_s=120, info=info,
                                   allow_unisolated=allow_unisolated, output=work / "secret-scan.log")
            result["secret_scan"] = ("clean" if scan["exit_code"] == 0 else
                                     "error" if scan["exit_code"] in (None, 125) else "findings")
        # the tests: the base commit's command, on the patched tree, in the sandbox
        if proof["test_command"]:
            say(f"  tests ({' '.join(proof['test_command'][:6])}…) in the sandbox")
            ran = lab_sandbox.run(_words(proof["test_command"], python), cwd=clone, work=work, python=python,
                                  env=env, timeout_s=proof["timeout_s"], info=info,
                                  allow_unisolated=allow_unisolated, output=work / "tests.log")
            result["tests"] = {"exit_code": ran["exit_code"], "timed_out": ran["timed_out"],
                               "seconds": ran["seconds"], **counts(ran["output_tail"])}
            result["sandbox"] = ran["sandbox"]
        else:
            result["tests"] = {"exit_code": None, "note": "the lab names no test command"}
        ok = (result["applied"] and result["files_match"] and result["secret_scan"] in ("clean", "skipped")
              and result["tests"].get("exit_code") == 0)
        result["verdict"] = "pass" if ok else "fail"
        if not ok and not result["problem"]:
            result["problem"] = ("the files it touches are not the ones it claims" if not result["files_match"] else
                                 "the secret scan found something" if result["secret_scan"] == "findings" else
                                 "the secret scan could not run" if result["secret_scan"] == "error" else
                                 "the tests timed out" if result["tests"].get("timed_out") else
                                 "the tests failed" if proof["test_command"] else "no tests to run")
        return result
    except lab_sandbox.SandboxUnavailable:
        raise
    except lab_proof.GitError as e:
        result["problem"] = f"git: {str(e)[:200]}"
        return result
    finally:
        result["seconds"] = round(time.monotonic() - t0, 1)
        logs = {n: (work / n) for n in ("tests.log", "secret-scan.log")}
        kept = _private_dir(root / "logs") / str(patch.get("patch_id"))
        _private_dir(kept)
        for name, p in logs.items():
            if p.exists():
                shutil.copyfile(p, kept / name)
        (kept / "result.json").write_text(json.dumps(result, indent=2))
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
