"""Labs (W0): a room bound to a repo, signed patches kept as data, the checker, a person landing.

Every repo and patch here is synthetic: a throwaway git repository with a two-line bug and its
test, a patch that fixes it, and patches that try what a patch should not."""
import json
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from emporia import lab_check, lab_proof, lab_sandbox, labs, operator_desk, playbook, signing, translog, wire
from emporia import relay_server as rs
from emporia import rooms as rooms_db
from tests.test_bugs import file as file_bug
from tests.test_emporia import _register_keyed

REPO = Path(__file__).resolve().parent.parent
SANDBOX = lab_sandbox.probe()
ISOLATED = SANDBOX["backend"] != "none"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EMPORIA_LABS", "1")
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,bugs")
    monkeypatch.setenv("EMPORIA_LAB_HOSTS", "file")
    return TestClient(rs.app, client=("127.0.0.1", 51310))


def as_(agent):
    return {"x-emporia-agent-id": agent}


def name(tag):
    return f"lb_{tag}_{uuid.uuid4().hex[:6]}"


def sh(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@localhost", "-c", "commit.gpgsign=false",
                           *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def stable_of(agent):
    with rs.DB_LOCK, rs.get_db() as conn:
        return conn.execute("SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (agent,)).fetchone()[0]


PROOF_EXTRA = {"test_command": ["{python}", "-m", "unittest", "-q"], "secret_scan": "scripts/secret_scan.py",
               "sensitive_paths": [".emporia/", "scripts/"], "timeout_s": 120}


def make_repo(tmp_path, slug, creator, relay=None, **proof_over):
    repo = tmp_path / f"repo-{slug}"
    repo.mkdir()
    sh("init", "-q", "-b", "main", cwd=repo)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "test_calc.py").write_text("import unittest\n\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n"
                                       "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
    (repo / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "secret_scan.py", repo / "scripts" / "secret_scan.py")
    (repo / ".emporia").mkdir()
    proof = {"type": labs.PROOF_TYPE, "relay": relay or rs.relay_id(), "lab": slug, "creator": creator,
             **PROOF_EXTRA, **proof_over}
    (repo / ".emporia" / "lab.json").write_text(json.dumps(proof, indent=1))
    sh("add", "-A", cwd=repo)
    sh("commit", "-q", "-m", "a calculator with a bug", cwd=repo)
    return repo, sh("rev-parse", "HEAD", cwd=repo)


def diff_of(repo, change):
    """The diff that `change(repo)` makes, then the tree put back."""
    change(repo)
    sh("add", "-A", cwd=repo)
    d = subprocess.run(["git", "diff", "--cached"], cwd=repo, check=True, capture_output=True, text=True).stdout
    sh("reset", "-q", "--hard", "HEAD", cwd=repo)
    sh("clean", "-qfd", cwd=repo)
    return d


def fix(repo):
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")


def make_lab(client, tmp_path, monkeypatch, slug=None, **proof_over):
    slug = slug or f"lab{uuid.uuid4().hex[:6]}"
    op = name("op")
    _register_keyed(client, op)
    monkeypatch.setenv("EMPORIA_AGENT_ID", op)
    repo, commit = make_repo(tmp_path, slug, stable_of(op), **proof_over)
    room = rooms_db.create_room(f"lab: {slug}", op, db_path=rs.DATABASE_PATH)
    return op, slug, repo, commit, room.room_id


def post_lab(client, op, slug, repo, commit, room_id, sign_as=None, checkers=None):
    at = datetime.now(UTC).isoformat()
    url = f"file://{repo}"
    checkers = checkers or [op]
    sig = signing.sign(sign_as or op, "lab", slug, slug, room_id, url, "main", commit, checkers, "MIT", op, at)
    return client.post("/labs", headers=as_(op), json={
        "slug": slug, "name": slug, "room_id": room_id, "repo_url": url, "branch": "main", "commit": commit,
        "checkers": checkers, "licence": "MIT", "at": at, "signature": sig})


def send_patch(client, agent, slug, thread, base, diff, files=None, summary="fix add: + not -", nonce=None):
    files = files if files is not None else labs.parse_diff(diff)[0]
    nonce = nonce or uuid.uuid4().hex
    at = datetime.now(UTC).isoformat()
    sig = signing.sign(agent, "lab-patch", slug, thread, base, labs.sha256(diff), sorted(files), labs.sha256(summary),
                       nonce, agent, at)
    return client.post(f"/labs/{slug}/patches", headers=as_(agent), json={
        "thread": thread, "base_commit": base, "diff": diff, "files": files, "summary": summary, "nonce": nonce,
        "at": at, "signature": sig})


def send_check(client, checker, slug, patch, result):
    at = datetime.now(UTC).isoformat()
    sig = signing.sign(checker, "lab-check", patch["patch_id"], patch["diff_sha256"], patch["base_commit"], result,
                       checker, at)
    return client.post(f"/labs/{slug}/patches/{patch['patch_id']}/checks", headers=as_(checker),
                       json={"result": result, "at": at, "signature": sig})


def a_lab_with_a_bug(client, tmp_path, monkeypatch):
    op, slug, repo, commit, room_id = make_lab(client, tmp_path, monkeypatch)
    assert post_lab(client, op, slug, repo, commit, room_id).status_code == 200
    alpha, beta = name("alpha"), name("beta")
    for a in (alpha, beta):
        _register_keyed(client, a)
    r, *_ = file_bug(client, alpha, sealed=False)
    assert r.status_code == 200, r.text
    return op, slug, repo, commit, room_id, alpha, beta, r.json()["report_id"]


# ── the rules, as text ──────────────────────────────────────────────────────

def test_a_repo_url_is_https_on_an_allowed_host_in_one_form():
    hosts = labs.DEFAULT_HOSTS
    assert labs.normalise_repo_url("https://github.com/o/r.git/", hosts) == ("https://github.com/o/r", "")
    for bad in ("http://github.com/o/r", "https://user:pw@github.com/o/r", "https://github.com/o/r?x=1",
                "https://evil.example/o/r", "https://github.com/o", "file:///tmp/r", "https://github.com/../r",
                "git@github.com:o/r.git", "https://github.com:8443/o/r"):
        assert labs.normalise_repo_url(bad, hosts)[1], bad
    assert labs.normalise_repo_url("file:///srv/r/", ("file",)) == ("file:///srv/r", "")


def test_a_diff_is_text_with_plain_paths_and_its_files_are_what_it_claims():
    d = ("diff --git a/calc.py b/calc.py\nindex 1..2 100644\n--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
         " def add(a, b):\n-    return a - b\n+    return a + b\n")
    files, facts, why = labs.parse_diff(d)
    assert (files, facts["added"], facts["removed"], why) == (["calc.py"], 1, 1, "")
    commit = "a" * 40
    assert labs.check_patch("emporia", "bug_0123456789abcdef", commit, d, ["calc.py"], "fix")[2] == ""
    assert "exactly the files" in labs.check_patch("emporia", "bug_0123456789abcdef", commit, d,
                                                   ["calc.py", "other.py"], "fix")[2]
    assert "exactly the files" in labs.check_patch("emporia", "bug_0123456789abcdef", commit, d, [], "fix")[2]
    assert labs.check_patch("emporia", "bug_0123456789abcdef", "abc123", d, ["calc.py"], "fix")[2]
    for bad in (d.replace("a/calc.py b/calc.py", "a/../x b/../x"),
                d.replace("a/calc.py b/calc.py", "a/.git/config b/.git/config"),
                "diff --git a/x.png b/x.png\nGIT binary patch\nliteral 3\n",
                "diff --git a/l b/l\nnew file mode 120000\n--- /dev/null\n+++ b/l\n@@ -0,0 +1 @@\n+/etc/passwd\n",
                "diff --git a/m b/m\nnew file mode 160000\n",
                d.replace("+++ b/calc.py", "+++ b/elsewhere.py"),
                "just some text", "x" * (labs.MAX_DIFF_BYTES + 1)):
        assert labs.parse_diff(bad)[2], bad[:60]


def test_the_proof_file_names_this_lab_and_carries_the_checkers_rules():
    good = {"type": labs.PROOF_TYPE, "relay": "r1", "lab": "emporia", "creator": "sid", **PROOF_EXTRA}
    proof, why = labs.parse_proof(json.dumps(good))
    assert why == "" and proof["test_command"][0] == "{python}" and proof["timeout_s"] == 120
    assert labs.proof_names(proof, "r1", "emporia", "sid") == ""
    assert "relay" in labs.proof_names(proof, "r2", "emporia", "sid")
    assert "creator" in labs.proof_names(proof, "r1", "emporia", "other")
    assert "FILL" not in labs.proof_names(proof, "r1", "emporia", "other")
    for bad in ({**good, "type": "x"}, {**good, "test_command": "pytest"}, {**good, "secret_scan": "../s.py"},
                {**good, "env": {"GITHUB_TOKEN": "x"}}, {**good, "env": {"AWS_REGION": "x"}}, {**good, "timeout_s": 5}):
        assert labs.parse_proof(json.dumps(bad))[1], bad


def test_this_repos_own_proof_file_is_well_formed_and_waits_for_the_operator():
    proof, why = labs.parse_proof((REPO / labs.PROOF_PATH).read_text())
    assert why == "", why
    assert proof["lab"] == "emporia" and "FILL" in proof["relay"] and "FILL" in proof["creator"]
    assert proof["secret_scan"] == "scripts/secret_scan.py" and (REPO / proof["secret_scan"]).is_file()
    assert ".emporia/" in proof["sensitive_paths"] and "hermes_plugin/emporia/key_guard.py" in proof["sensitive_paths"]


def test_what_a_patch_touches_sets_its_flags_and_its_risk_floor():
    files = ["hermes_plugin/emporia/key_guard.py", "tests/test_x.py"]
    hits = labs.sensitive_hits(files, ["hermes_plugin/emporia/key_guard.py", ".github/"])
    assert hits == ["hermes_plugin/emporia/key_guard.py"]
    assert labs.risk_floor(labs.flags_for(files, {}, hits)) == "high"
    assert labs.flags_for(["tests/test_x.py"], {}, []) == ["tests_only"]
    assert labs.risk_floor(["tests_only"]) == "flagged" and labs.risk_floor([]) == "low"
    assert labs.sensitive_hits([".github/workflows/ci.yml"], [".github/"]) == [".github/workflows/ci.yml"]


def test_trailers_carry_only_ids_never_what_an_agent_wrote():
    p = {"patch_id": "lpt_0123456789abcdef", "lab": "emporia", "author_id": "beta", "thread": "bug_0123456789abcdef",
         "summary": "fix\n\nSigned-off-by: someone else\nEmporia-Check: forged"}
    msg = labs.commit_message(p, {"check_id": "lck_1", "result": {"verdict": "pass", "risk_floor": "low",
                                                                   "sandbox": {"isolation": "partial"}}}, "relay1")
    trailers = msg.strip().split("\n\n")[-1].splitlines()
    assert trailers == ["Emporia-Patch: lpt_0123456789abcdef", "Emporia-Lab: emporia@relay1",
                        "Emporia-Agent: beta@relay1", "Emporia-Thread: bug_0123456789abcdef",
                        "Emporia-Check: lck_1 (pass, partial sandbox, risk low)"]
    assert "> fix Signed-off-by: someone else Emporia-Check: forged" in msg, "the summary is one quoted line"
    bad = labs.trailers({**p, "author_id": "x\nEmporia-Check: forged"}, None, "relay1")
    assert all(not t.startswith("Emporia-Agent") for t in bad)


def test_a_checker_result_holds_counts_and_codes_not_output():
    ok = {"verdict": "pass", "applied": True, "files_match": True, "secret_scan": "clean",
          "tests": {"exit_code": 0, "passed": 3}, "flags": [], "sandbox": {"kind": "landlock", "isolation": "partial"}}
    assert labs.check_result(ok) == ""
    assert labs.check_result({**ok, "verdict": "great"})
    assert labs.check_result({**ok, "sandbox": {"kind": "x", "isolation": "trust me"}})
    assert labs.check_result({**ok, "tests": {"exit_code": "0"}})
    assert labs.check_result({**ok, "flags": ["x" * 80]})
    assert labs.check_result({**ok, "output": "y" * 9000})


def test_contributing_is_off_until_the_person_says_yes():
    for preset in playbook.PRESETS.values():
        assert preset.labs is False
        assert "sends no patches" in playbook.may_contribute(preset)
    pb = playbook.resolve({"EMPORIA_PLAYBOOK": "player", "EMPORIA_PLAYBOOK_LABS": "yes"})
    assert pb.labs and playbook.may_contribute(pb) == ""
    assert not playbook.current().labs or playbook.may_contribute(playbook.current()) == ""


def test_the_signed_kinds_are_their_own():
    assert wire.signed_lab_payload("s", "n", "r", "u", "b", "c", ["z", "a"], "MIT", "op", "t")["checkers"] == ["a", "z"]
    for kind, t in (("lab", "emporia:lab:v1"), ("lab-patch", "emporia:lab-patch:v1"),
                    ("lab-check", "emporia:lab-check:v1")):
        assert getattr(wire, signing.KINDS[kind].builder).__name__.startswith("signed_lab")
        assert not signing.KINDS[kind].person
        assert t.startswith("emporia:lab")


# ── the relay ──────────────────────────────────────────────────────────────

def test_off_unless_turned_on_and_never_without_the_log(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_LABS", "0")
    assert client.get("/labs").status_code == 404
    monkeypatch.setenv("EMPORIA_LABS", "1")
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "off")
    r = client.get("/labs")
    assert r.status_code == 404 and "needs the log module" in r.json()["detail"]
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    assert client.get("/relay/card").json()["relay_modules"]["labs"]["parts"] == ["patches"]


def test_only_the_operator_makes_a_lab_and_only_when_the_repo_says_so(client, tmp_path, monkeypatch):
    op, slug, repo, commit, room_id = make_lab(client, tmp_path, monkeypatch)
    other = name("other")
    _register_keyed(client, other)
    at = datetime.now(UTC).isoformat()
    sig = signing.sign(other, "lab", slug, slug, room_id, f"file://{repo}", "main", commit, [other], "MIT", other, at)
    r = client.post("/labs", headers=as_(other), json={"slug": slug, "name": slug, "room_id": room_id,
                                                        "repo_url": f"file://{repo}", "branch": "main",
                                                        "commit": commit, "checkers": [other], "licence": "MIT",
                                                        "at": at, "signature": sig})
    assert r.status_code == 403, "W0: the relay operator only"
    assert post_lab(client, op, slug, repo, commit, room_id, sign_as=other).status_code == 403, "its own signature"
    # the repo moved on: the pin is not the tip
    (repo / "README").write_text("later\n")
    sh("add", "-A", cwd=repo)
    sh("commit", "-q", "-m", "later", cwd=repo)
    r = post_lab(client, op, slug, repo, commit, room_id)
    assert r.status_code == 422 and "not the pinned" in r.json()["detail"]
    tip = sh("rev-parse", "HEAD", cwd=repo)
    r = post_lab(client, op, slug, repo, tip, room_id)
    assert r.status_code == 200, r.text
    lab = r.json()
    assert lab["represents"] and lab["kind"] == "lab" and lab["room_id"] == room_id
    assert post_lab(client, op, slug, repo, tip, room_id).status_code == 409


def test_a_repo_that_names_another_lab_or_relay_is_refused(client, tmp_path, monkeypatch):
    op, slug, repo, commit, room_id = make_lab(client, tmp_path, monkeypatch, relay="FILL_IN_THE_RELAY_ID")
    r = post_lab(client, op, slug, repo, commit, room_id)
    assert r.status_code == 422 and "names relay" in r.json()["detail"] and "operator fills it in" in r.json()["detail"]
    other_slug = f"lab{uuid.uuid4().hex[:6]}"
    r = post_lab(client, op, other_slug, repo, commit, room_id)
    assert r.status_code == 422 and "names relay" in r.json()["detail"]
    monkeypatch.setenv("EMPORIA_LAB_HOSTS", "github.com")
    r = post_lab(client, op, slug, repo, commit, room_id)
    assert r.status_code == 422 and "test relay" in r.json()["detail"]


def test_a_lab_room_is_a_public_room_its_maker_made(client, tmp_path, monkeypatch):
    op, slug, repo, commit, _room = make_lab(client, tmp_path, monkeypatch)
    other_room = rooms_db.create_room("someone else's", "someone", db_path=rs.DATABASE_PATH)
    assert post_lab(client, op, slug, repo, commit, other_room.room_id).status_code == 422
    private = rooms_db.create_room("private", op, room_type="private", gate_type="invite", db_path=rs.DATABASE_PATH)
    assert post_lab(client, op, slug, repo, commit, private.room_id).status_code == 422


def test_a_patch_is_signed_data_bound_to_a_bug_with_its_own_thread_in_the_lab_room(client, tmp_path, monkeypatch):
    op, slug, repo, commit, room_id, alpha, beta, bug = a_lab_with_a_bug(client, tmp_path, monkeypatch)
    d = diff_of(repo, fix)
    r = send_patch(client, beta, slug, bug, commit, d)
    assert r.status_code == 200, r.text
    p = r.json()
    assert (p["status"], p["files"], p["author_id"], p["thread"]) == ("open", ["calc.py"], beta, bug)
    assert "never" not in p["note"] or "lands" in p["note"]
    got = client.get(f"/labs/{slug}/patches/{p['patch_id']}").json()
    assert got["diff"] == d and got["diff_sha256"] == labs.sha256(d) and got["checks"] == []
    listed = client.get(f"/labs/{slug}/patches").json()["patches"]
    assert [x["patch_id"] for x in listed] == [p["patch_id"]] and "diff" not in listed[0]
    # its thread: the relay's note in the lab's room, which discussion replies to
    msgs = rooms_db.get_messages(room_id, db_path=rs.DATABASE_PATH)
    root = [m for m in msgs if m.message_id == p["thread_message_id"]]
    assert root and root[0].sender_id == "emporia:lab" and p["patch_id"] in root[0].content
    assert "fix add" not in root[0].content, "the agent's summary is not put in the relay's own words"
    # in the transparency log at once: the diff's hash, not the diff
    with rs.DB_LOCK, rs.get_db() as conn:
        translog.fold_all(conn)
        [entry] = [e["record"] for e in translog.entries(conn, 0, ref=f"lab_patch:{p['patch_id']}", limit=10)]
    assert entry["diff_sha256"] == labs.sha256(d) and "return a + b" not in json.dumps(entry)


def test_a_patch_is_refused_off_the_pin_off_a_thread_off_its_signature_or_off_its_claim(client, tmp_path, monkeypatch):
    op, slug, repo, commit, room_id, alpha, beta, bug = a_lab_with_a_bug(client, tmp_path, monkeypatch)
    d = diff_of(repo, fix)
    assert send_patch(client, beta, slug, bug, "b" * 40, d).status_code == 409
    assert send_patch(client, beta, slug, "bug_ffffffffffffffff", commit, d).status_code == 422
    assert send_patch(client, beta, slug, "a-post-nobody-adopted", commit, d).status_code == 422
    assert send_patch(client, beta, slug, bug, commit, d, files=["calc.py", "x.py"]).status_code == 422
    nonce = uuid.uuid4().hex
    at = datetime.now(UTC).isoformat()
    sig = signing.sign(alpha, "lab-patch", slug, bug, commit, labs.sha256(d), ["calc.py"], labs.sha256("s"), nonce,
                       beta, at)
    r = client.post(f"/labs/{slug}/patches", headers=as_(beta), json={
        "thread": bug, "base_commit": commit, "diff": d, "files": ["calc.py"], "summary": "s", "nonce": nonce,
        "at": at, "signature": sig})
    assert r.status_code == 403, "signed by someone else"
    ok = send_patch(client, beta, slug, bug, commit, d, nonce="samenonce123")
    assert ok.status_code == 200
    assert send_patch(client, beta, slug, bug, commit, d, nonce="samenonce123").status_code == 409, "sent once"


def test_patches_are_capped_per_operator_and_per_agent(client, tmp_path, monkeypatch):
    op, slug, repo, commit, room_id, alpha, beta, bug = a_lab_with_a_bug(client, tmp_path, monkeypatch)
    d = diff_of(repo, fix)
    for _ in range(labs.OPEN_CAP):
        assert send_patch(client, beta, slug, bug, commit, d).status_code == 200
    r = send_patch(client, beta, slug, bug, commit, d)
    assert r.status_code == 429 and "already open" in r.json()["detail"]
    assert labs.may_submit(0, labs.DAILY_CAP)


def test_only_a_checker_the_lab_names_signs_a_result_and_it_is_posted_in_the_thread(client, tmp_path, monkeypatch):
    op, slug, repo, commit, room_id, alpha, beta, bug = a_lab_with_a_bug(client, tmp_path, monkeypatch)
    p = send_patch(client, beta, slug, bug, commit, diff_of(repo, fix)).json()
    result = {"verdict": "pass", "applied": True, "files_match": True, "secret_scan": "clean",
              "tests": {"exit_code": 0, "passed": 1, "failed": 0}, "flags": [], "risk_floor": "low",
              "sandbox": {"kind": "landlock-abi4+seccomp", "isolation": "partial", "network": "none"}}
    assert send_check(client, beta, slug, p, result).status_code == 403, "the author is no checker"
    assert send_check(client, op, slug, p, {**result, "verdict": "maybe"}).status_code == 422
    r = send_check(client, op, slug, p, result)
    assert r.status_code == 200, r.text
    got = client.get(f"/labs/{slug}/patches/{p['patch_id']}").json()
    assert got["status"] == "passed" and got["checks"][0]["result"]["sandbox"]["isolation"] == "partial"
    msgs = rooms_db.get_messages(room_id, db_path=rs.DATABASE_PATH)
    reply = [m for m in msgs if m.parent_message_id == p["thread_message_id"]]
    assert reply and "pass" in reply[0].content and "partial sandbox" in reply[0].content
    desk = client.get("/operator/status", headers=as_(op)).json()["patches"]
    [item] = [i for i in desk["items"] if i["patch_id"] == p["patch_id"]]
    assert item["check"]["verdict"] == "pass" and item["check"]["isolation"] == "partial"
    cmds = [c["command"] for c in operator_desk.patch_commands("opdesk", item)]
    assert cmds[0] == f"hermes emporia lab opdesk land {p['patch_id']} --into CHECKOUT"
    text = "\n".join(operator_desk.render(client.get("/operator/status", headers=as_(op)).json(), "opdesk"))
    assert "Patches waiting" in text and p["patch_id"] in text and "(the agent's words)" in text
    assert client.get("/operator/status", headers=as_(beta)).status_code == 403


# ── the checker, in its sandbox ─────────────────────────────────────────────

def lab_record(client, slug, op):
    return {**client.get(f"/labs/{slug}").json(), "relay_id": rs.relay_id(), "creator_stable_id": stable_of(op)}


def run_check(client, slug, op, patch_id, tmp_path, **kw):
    patch = client.get(f"/labs/{slug}/patches/{patch_id}").json()
    return lab_check.check_patch(lab_record(client, slug, op), patch, root=tmp_path / "checker", allow_file=True,
                                 allow_unisolated=not ISOLATED, sandbox_info=SANDBOX, **kw)


def test_the_checker_passes_a_fix_in_its_sandbox_and_fails_the_bug(client, tmp_path, monkeypatch):
    op, slug, repo, commit, room_id, alpha, beta, bug = a_lab_with_a_bug(client, tmp_path, monkeypatch)
    good = send_patch(client, beta, slug, bug, commit, diff_of(repo, fix)).json()
    res = run_check(client, slug, op, good["patch_id"], tmp_path)
    assert res["verdict"] == "pass", res
    assert res["applied"] and res["files_match"] and res["secret_scan"] == "clean" and res["represents"]
    assert res["tests"]["exit_code"] == 0 and res["tests"]["passed"] == 1 and res["risk_floor"] == "low"
    assert res["sandbox"]["isolation"] == ("partial" if SANDBOX["backend"] == "landlock" else
                                           "isolated" if SANDBOX["backend"] == "bwrap" else "none")
    assert labs.check_result(res) == ""
    assert send_check(client, op, slug, good, res).status_code == 200

    def noop(r):
        (r / "NOTES").write_text("nothing fixed\n")
    bad = send_patch(client, beta, slug, bug, commit, diff_of(repo, noop), summary="does nothing").json()
    res = run_check(client, slug, op, bad["patch_id"], tmp_path)
    assert res["verdict"] == "fail" and res["tests"]["exit_code"] != 0 and "tests failed" in res["problem"]
    assert not list((tmp_path / "checker" / "runs").iterdir()), "each work folder is removed"
    assert (tmp_path / "checker" / "logs" / bad["patch_id"] / "tests.log").exists(), "its log is kept locally"


def test_the_rules_come_from_the_base_commit_never_the_patch(client, tmp_path, monkeypatch):
    op, slug, repo, commit, room_id, alpha, beta, bug = a_lab_with_a_bug(client, tmp_path, monkeypatch)

    def cheat(r):
        proof = json.loads((r / ".emporia" / "lab.json").read_text())
        proof["test_command"] = ["true"]
        (r / ".emporia" / "lab.json").write_text(json.dumps(proof))
        (r / "scripts" / "secret_scan.py").write_text("import sys\nsys.exit(0)\n")
    p = send_patch(client, beta, slug, bug, commit, diff_of(repo, cheat), summary="tidy config").json()
    res = run_check(client, slug, op, p["patch_id"], tmp_path)
    assert res["verdict"] == "fail" and res["tests"]["exit_code"] != 0, "the base commit's tests still ran"
    assert "sensitive_paths" in res["flags"] and res["risk_floor"] == "high"
    assert sorted(res["sensitive"]) == [".emporia/lab.json", "scripts/secret_scan.py"]


def test_the_base_commits_secret_scanner_finds_a_secret_a_patch_adds(client, tmp_path, monkeypatch):
    op, slug, repo, commit, room_id, alpha, beta, bug = a_lab_with_a_bug(client, tmp_path, monkeypatch)

    def leak(r):
        fix(r)
        token = "ghp_" + "A1b2C3d4" * 5
        (r / "config.py").write_text(f"TOKEN = '{token}'\n")
    p = send_patch(client, beta, slug, bug, commit, diff_of(repo, leak)).json()
    res = run_check(client, slug, op, p["patch_id"], tmp_path)
    assert res["secret_scan"] == "findings" and res["verdict"] == "fail" and "secret scan" in res["problem"]
    assert "ghp_" not in json.dumps(res), "the result never holds what it found"


@pytest.mark.skipif(SANDBOX["backend"] != "landlock", reason="Landlock is not available here")
def test_in_the_sandbox_a_patch_reaches_no_network_no_home_and_no_keys(client, tmp_path, monkeypatch):
    op, slug, repo, commit, room_id, alpha, beta, bug = a_lab_with_a_bug(client, tmp_path, monkeypatch)
    probe = tmp_path / "outside-secret.txt"
    probe.write_text("a secret outside the work folder\n")

    def snoop(r):
        fix(r)
        (r / "test_snoop.py").write_text(
            "import os, socket, unittest\n\n\nclass S(unittest.TestCase):\n"
            "    def test_no_network(self):\n"
            "        with self.assertRaises(OSError):\n"
            "            socket.create_connection(('127.0.0.1', 8088), timeout=2)\n"
            "        with self.assertRaises(OSError):\n"
            "            socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
            "        with self.assertRaises(OSError):\n"
            "            socket.socket(socket.AF_UNIX)\n\n"
            "    def test_no_files_outside(self):\n"
            f"        with self.assertRaises(OSError):\n            open({str(probe)!r}).read()\n"
            "        with self.assertRaises(OSError):\n            open('/proc/1/environ').read()\n"
            f"        with self.assertRaises(OSError):\n            os.listdir({str(Path.home())!r})\n\n"
            "    def test_no_secrets_in_its_environment(self):\n"
            "        names = set(os.environ)\n"
            "        self.assertFalse({n for n in names if 'KEY' in n or 'TOKEN' in n or n.startswith('EMPORIA_ID')})\n"
            "        self.assertEqual(os.environ['EMPORIA_LAB_SANDBOX'], '1')\n")
    p = send_patch(client, beta, slug, bug, commit, diff_of(repo, snoop), summary="fix, and a test").json()
    res = run_check(client, slug, op, p["patch_id"], tmp_path)
    log = (tmp_path / "checker" / "logs" / p["patch_id"] / "tests.log").read_text()
    assert res["verdict"] == "pass" and res["tests"]["passed"] == 4, log
    assert res["sandbox"]["kind"].startswith("landlock") and res["sandbox"]["gaps"]


def test_without_a_sandbox_nothing_runs_unless_the_person_allows_it(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    with pytest.raises(lab_sandbox.SandboxUnavailable):
        lab_sandbox.run(["true"], cwd=work, work=work, python=sys.executable, env={"PATH": "/usr/bin:/bin"},
                        timeout_s=10, info={"backend": "none"})
    out = lab_sandbox.run(["true"], cwd=work, work=work, python=sys.executable, env={"PATH": "/usr/bin:/bin"},
                          timeout_s=10, info={"backend": "none"}, allow_unisolated=True)
    assert out["exit_code"] == 0 and out["sandbox"]["isolation"] == "none" and "NOT ISOLATED" in out["sandbox"]["gaps"][0]


def test_a_test_run_past_its_time_is_killed_with_its_children(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    out = lab_sandbox.run([sys.executable, "-c", "import subprocess, time; subprocess.Popen(['sleep', '60']); "
                                                 "time.sleep(60)"],
                          cwd=work, work=work, python=sys.executable, env=lab_sandbox.sandbox_env(work, sys.executable),
                          timeout_s=2, info=SANDBOX, allow_unisolated=True)
    assert out["timed_out"] and out["exit_code"] is None and out["seconds"] < 30


def test_a_reader_rechecks_a_pin_the_branch_has_moved_past_and_sees_it_lapse(tmp_path):
    repo, commit = make_repo(tmp_path, "emporia", "sid", relay="r1")
    url = f"file://{repo}"
    ok = lab_proof.check(url, "main", commit, relay="r1", slug="emporia", creator="sid", allow_file=True)
    assert ok["represents"] and ok["tip"] == commit
    (repo / "README").write_text("later\n")
    sh("add", "-A", cwd=repo)
    sh("commit", "-q", "-m", "later", cwd=repo)
    assert not lab_proof.check(url, "main", commit, relay="r1", slug="emporia", creator="sid",
                               allow_file=True)["represents"], "at creation the pin is the tip"
    again = lab_proof.check(url, "main", commit, relay="r1", slug="emporia", creator="sid", allow_file=True,
                            recheck=True)
    assert again["represents"] and not again["lapsed"]
    sh("rm", "-q", ".emporia/lab.json", cwd=repo)
    sh("commit", "-q", "-m", "not a lab any more", cwd=repo)
    gone = lab_proof.check(url, "main", commit, relay="r1", slug="emporia", creator="sid", allow_file=True,
                           recheck=True)
    assert not gone["represents"] and gone["lapsed"] and "lapsed" in gone["why"]
    assert not lab_proof.check(url, "main", "c" * 40, relay="r1", slug="emporia", creator="sid", allow_file=True,
                               recheck=True)["represents"], "a commit not on the branch"


# ── a person lands it ───────────────────────────────────────────────────────

def test_landing_makes_one_commit_on_a_new_branch_and_touches_nothing_else(client, tmp_path, monkeypatch):
    op, slug, repo, commit, room_id, alpha, beta, bug = a_lab_with_a_bug(client, tmp_path, monkeypatch)
    p = send_patch(client, beta, slug, bug, commit, diff_of(repo, fix)).json()
    patch = client.get(f"/labs/{slug}/patches/{p['patch_id']}").json()
    checkout = tmp_path / "mine"
    subprocess.run(["git", "clone", "-q", str(repo), str(checkout)], check=True)
    (checkout / "scratch.txt").write_text("my own work in progress\n")
    hook = checkout / ".git" / "hooks" / "post-commit"
    hook.write_text("#!/bin/sh\ntouch HOOK_RAN\n")
    hook.chmod(0o755)
    check = {"check_id": "lck_0123456789abcdef", "result": {"verdict": "pass", "risk_floor": "low",
                                                            "sandbox": {"isolation": "partial"}}}
    out = lab_check.land_patch(checkout, patch, check, "relay1")
    assert out["branch"] == f"lab/{p['patch_id']}" and out["base"] == commit
    msg = sh("log", "-1", "--format=%B", out["branch"], cwd=checkout)
    assert f"Emporia-Patch: {p['patch_id']}" in msg and f"Emporia-Agent: {beta}@relay1" in msg
    assert f"Emporia-Thread: {bug}" in msg and "Emporia-Check: lck_0123456789abcdef (pass" in msg
    assert sh("show", f"{out['branch']}:calc.py", cwd=checkout).endswith("return a + b")
    assert sh("rev-parse", "--abbrev-ref", "HEAD", cwd=checkout) == "main", "the current branch is unchanged"
    assert (checkout / "calc.py").read_text().endswith("a - b\n"), "the working tree is unchanged"
    assert (checkout / "scratch.txt").exists() and not (checkout / "HOOK_RAN").exists(), "no hook ran"
    with pytest.raises(lab_proof.GitError, match="already"):
        lab_check.land_patch(checkout, patch, check, "relay1")


# ── who may run what ────────────────────────────────────────────────────────

def test_the_key_guard_keeps_agents_off_the_checker_its_folder_and_github_tokens():
    from hermes_plugin.emporia import key_guard as guard
    for cmd in ("hermes emporia lab opdesk check emporia", "emporia lab check opdesk",
                "hermes emporia lab opdesk land lpt_1 --into ~/src/emporia", "emporia lab opdesk create emporia --repo x",
                "python -m emporia.lab_check", "python -m emporia.lab_sandbox spec.json",
                "ls ~/.cache/emporia-lab-check/runs", "echo $GITHUB_TOKEN", "gh auth token", "cat ~/.config/gh/hosts.yml"):
        v = guard.pre_tool_call("terminal", {"command": cmd})
        assert v and v["action"] == "block", cmd
    for cmd in ("hermes emporia lab opdesk show emporia", "hermes emporia lab opdesk list", "git diff"):
        assert guard.pre_tool_call("terminal", {"command": cmd}) is None, cmd
    v = guard.pre_tool_call("read_file", {"path": "/home/x/.cache/emporia-lab-check/logs/lpt_1/tests.log"})
    assert v and v["action"] == "block"


def test_the_cli_takes_either_order_and_the_checker_refuses_an_agents_terminal(monkeypatch, capsys):
    from emporia import cli
    agents = {"opdesk": Path("/x")}
    assert cli._lab_words(["check", "opdesk"], agents) == ("opdesk", "check", [])
    assert cli._lab_words(["opdesk", "check", "emporia"], agents) == ("opdesk", "check", ["emporia"])
    assert cli._lab_words(["opdesk"], agents) == ("opdesk", "list", [])
    assert cli._lab_words(["opdesk", "land", "lpt_1"], agents) == ("opdesk", "land", ["lpt_1"])
    monkeypatch.setattr(cli, "_agent_profiles", lambda root: [(Path("/x/opdesk"), {})])
    monkeypatch.setattr(cli, "_agent", lambda n: (Path("/x/opdesk"), "opdesk", {"EMPORIA_AGENT_ID": "opdesk"}))
    monkeypatch.setenv("HERMES_SESSION_ID", "abc")
    args = cli.build_parser().parse_args(["lab", "check", "opdesk"])
    assert args.func(args) == 1 and "agent's terminal" in capsys.readouterr().out
    monkeypatch.delenv("HERMES_SESSION_ID")
    monkeypatch.setattr(cli, "_persons_terminal", lambda: False)
    for words in (["lab", "opdesk", "land", "lpt_1", "--into", "/tmp/x"], ["lab", "opdesk", "create", "emporia"]):
        args = cli.build_parser().parse_args(words)
        assert args.func(args) == 1 and "only a person" in capsys.readouterr().out


def test_doctor_calls_a_github_token_in_a_profile_an_error(tmp_path):
    from emporia import cli
    profile = tmp_path / "p"
    (profile / "home" / ".config" / "gh").mkdir(parents=True)
    rep = cli.Report(compact=False)
    cli.doctor_no_github_token(rep, profile, {"GITHUB_TOKEN": "ghp_" + "x" * 36})
    assert rep.failed
    rep = cli.Report(compact=False)
    cli.doctor_no_github_token(rep, profile, {})
    assert not rep.failed
    (profile / "home" / ".config" / "gh" / "hosts.yml").write_text("github.com: {}\n")
    rep = cli.Report(compact=False)
    cli.doctor_no_github_token(rep, profile, {})
    assert rep.failed


def test_submit_patch_is_refused_while_the_playbook_says_no(monkeypatch):
    from emporia import mcp_server
    monkeypatch.setenv("EMPORIA_PLAYBOOK", "player")
    monkeypatch.delenv("EMPORIA_PLAYBOOK_LABS", raising=False)
    fn = getattr(mcp_server.submit_patch, "fn", mcp_server.submit_patch)
    out = fn("emporia", "bug_0123456789abcdef", "diff --git a/x b/x\n", ["x"], "s")
    assert not out["success"] and "sends no patches" in out["error"] and "--labs yes" in out["error"]


def test_a_lab_finding_is_a_patch_or_a_non_system_room_message_with_the_labs_creator(client, tmp_path, monkeypatch):
    from emporia.routes import standing
    op, slug, repo, commit, room_id = make_lab(client, tmp_path, monkeypatch)
    assert post_lab(client, op, slug, repo, commit, room_id).status_code in (200, 201)
    a = name("a")
    with rs.DB_LOCK, rs.get_db() as conn:
        for i, (who, kind) in enumerate(((a, "chat"), (a, "system"), ("someone_else", "chat"))):
            conn.execute("INSERT INTO room_messages (message_id, room_id, sender_id, msg_type, content, created_at) "
                         "VALUES (?, ?, ?, ?, 'x', ?)", (f"m{uuid.uuid4().hex}", room_id, who, kind, f"2026-11-0{i + 1}"))
    got = standing._lab_findings(a)
    assert [(f["creator"]) for f in got] == [op], "one chat message counts: not the system line, not another agent's"
