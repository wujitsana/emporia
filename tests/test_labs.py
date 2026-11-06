"""Labs v2: a room about a GitHub repo; signed claims; maintainers' signed reports; the quests and board.

Every repo here is a synthetic local git repository (`file://`, on a test relay); nothing touches GitHub
or the network, and the relay never fetches GitHub: it only sees signed text."""
import json
import os
import stat
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from emporia import labs, refs, signing, translog, wire
from emporia import relay_server as rs
from emporia import rooms as rooms_db
from tests.test_emporia import _register_keyed

REPO_NAME = "acme/widgets"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EMPORIA_LABS", "1")
    monkeypatch.setenv("EMPORIA_LAB_HOSTS", "file")
    monkeypatch.setenv("EMPORIA_QUEST_OTHERS", "any")
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    monkeypatch.setenv("EMPORIA_GAMIFY", "quests")
    monkeypatch.delenv("EMPORIA_HOUSE_AGENTS", raising=False)
    return TestClient(rs.app, client=("127.0.0.1", 51311))


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


def same_operator(*agents):
    """Make these agents one operator (one Nous account), as the relay counts them."""
    tag = uuid.uuid4().hex[:8]
    with rs.DB_LOCK, rs.get_db() as conn:
        for a in agents:
            conn.execute("UPDATE authorized_agents SET nous_user_id = ? WHERE agent_id = ?", (f"u{tag}", a))
        conn.commit()


def make_repo(tmp_path, slug, creator, maintainers, relay=None):
    repo = tmp_path / f"repo-{slug}"
    repo.mkdir()
    sh("init", "-q", "-b", "main", cwd=repo)
    (repo / ".emporia").mkdir()
    (repo / ".emporia" / "lab.json").write_text(json.dumps({
        "type": labs.PROOF_TYPE, "relay": relay or rs.relay_id(), "lab": slug, "creator": creator,
        "maintainers": maintainers}))
    (repo / "README").write_text("widgets\n")
    sh("add", "-A", cwd=repo)
    sh("commit", "-q", "-m", "widgets", cwd=repo)
    return repo, sh("rev-parse", "HEAD", cwd=repo)


def post_lab(client, creator, slug, repo, commit, room_id, maintainers, subject_ref=REPO_NAME, verified=True,
             repo_url=None):
    at = datetime.now(UTC).isoformat()
    url = repo_url if repo_url is not None else f"file://{repo}"
    subject = {"kind": "github_repo", "ref": subject_ref}
    sig = signing.sign(creator, "lab", slug, slug, room_id, subject, "main", commit, maintainers, "MIT",
                       stable_of(creator), at)
    return client.post("/labs", headers=as_(creator), json={
        "slug": slug, "name": slug, "room_id": room_id, "subject": subject, "repo_url": url, "branch": "main",
        "commit": commit, "maintainers": maintainers, "licence": "MIT", "verified": verified, "at": at,
        "signature": sig})


class Lab:
    """A lab on this relay with a maintainer, an author, a reviewer and a bystander, each their own operator."""

    def __init__(self, client, tmp_path, monkeypatch, room_kw=None):
        self.client, self.slug = client, f"lab{uuid.uuid4().hex[:6]}"
        self.creator, self.maint, self.author, self.reviewer, self.other = (
            name(t) for t in ("creator", "maint", "author", "reviewer", "other"))
        for a in (self.creator, self.maint, self.author, self.reviewer, self.other):
            _register_keyed(client, a)
        monkeypatch.setenv("EMPORIA_OPERATOR_STABLE_ID", stable_of(self.creator))   # the creator is the relay operator
        self.repo, self.commit = make_repo(tmp_path, self.slug, stable_of(self.creator), [stable_of(self.maint)])
        self.room = rooms_db.create_room(f"lab: {self.slug}", self.creator, db_path=rs.DATABASE_PATH,
                                         **(room_kw or {}))
        self.subject = f"owner{uuid.uuid4().hex[:5]}/widgets"

    def create(self, **kw):
        # the proof file is per lab; the subject names a repo of its own so labs on one relay do not collide
        return post_lab(self.client, self.creator, self.slug, self.repo, self.commit, self.room.room_id,
                        [stable_of(self.maint)], subject_ref=self.subject, **kw)

    def line(self, agent, kind, number, repo=None, at=None, head=""):
        at = at or datetime.now(UTC).isoformat()
        claim, sig = signing.signed(agent, "gh-claim", repo or self.subject, kind, number, head, stable_of(agent), at)
        return labs.claim_line(f"{agent}@e2e", claim, sig)

    def report(self, signer, ref, event, author_line, reviews=(), commit="", note=""):
        at = datetime.now(UTC).isoformat()
        blob = author_line.split()[-1]
        rv = [x.split()[-1] for x in reviews]
        sig = signing.sign(signer, "lab-report", self.slug, ref, event, commit, blob, rv, note, stable_of(signer), at)
        return self.client.post(f"/labs/{self.slug}/reports", headers=as_(signer), json={
            "ref": ref, "event": event, "commit": commit, "author_claim": blob, "credited_reviews": rv, "note": note,
            "at": at, "signature": sig})

    def pr(self, n):
        return f"gh_pr:{self.subject}#{n}"

    def issue(self, n):
        return f"gh_issue:{self.subject}#{n}"


@pytest.fixture
def lab(client, tmp_path, monkeypatch):
    lb = Lab(client, tmp_path, monkeypatch)
    r = lb.create()
    assert r.status_code == 200, r.text
    return lb


def done(agent):
    from emporia.routes import standing
    return {q["id"] for q in standing.quests_body(agent)["quests"] if q["done"]}


# ── the rules, as text ──────────────────────────────────────────────────────

def test_gh_refs_are_text_names_never_urls():
    assert refs.text("gh_pr:acme/widgets#12") == "gh_pr:acme/widgets#12"
    assert refs.parse("gh_issue:acme/widgets#3@relay.example")["relay"] == "relay.example"
    for bad in ("gh_pr:https://github.com/acme/widgets/pull/1", "gh_pr:acme/widgets", "gh_issue:acme/widgets#0",
                "gh_pr:acme#1", "gh_pr:a/b/c#1"):
        assert refs.problem(bad), bad
    assert wire.post_problem("note", "", "see it", [], reply_to="gh_pr:acme/widgets#12",
                             root="gh_pr:acme/widgets#12") == ""
    assert refs.parse("gh_pr:acme/widgets#12@r.example:8088#" + "a" * 64) == {
        "kind": "gh_pr", "id": "acme/widgets#12", "relay": "r.example:8088", "author": "a" * 64}
    assert refs.text(refs.parse("gh_issue:acme/widgets#3#" + "b" * 64)) == "gh_issue:acme/widgets#3#" + "b" * 64


def test_a_claim_line_carries_a_signed_claim_and_nothing_else(client):
    a = name("a")
    _register_keyed(client, a)
    claim, sig = signing.signed(a, "gh-claim", REPO_NAME, "pr", 7, "", stable_of(a), datetime.now(UTC).isoformat())
    line = labs.claim_line(f"{a}@e2e", claim, sig)
    assert line.startswith(f"Emporia: {a}@e2e ") and "\n" not in line
    got, got_sig, why = labs.decode_claim(f"text before\n{line}\nand after")
    assert not why and got == claim and got_sig == sig
    assert labs.decode_claim(line.split()[-1])[2] == ""
    assert labs.decode_claim("Emporia: x@y notbase64!")[2]
    bad = dict(claim, extra=1)
    assert labs.claim_problem(bad) and labs.claim_problem(dict(claim, repo="https://github.com/a/b"))
    assert labs.claim_problem(dict(claim, kind="merge")) and labs.claim_problem(dict(claim, number=0))
    assert labs.claim_matches(claim, REPO_NAME, "pr", 7) == ""
    assert labs.claim_matches(claim, "other/repo", "pr", 7) and labs.claim_matches(claim, REPO_NAME, "issue", 7)
    assert labs.claim_matches(claim, REPO_NAME, "pr", 8)


def test_the_proof_file_names_the_lab_its_creator_and_its_maintainers():
    text = json.dumps({"type": labs.PROOF_TYPE, "relay": "r", "lab": "l", "creator": "c" * 64,
                       "maintainers": ["m" * 64, "n" * 64]})
    proof, why = labs.parse_proof(text)
    assert not why and labs.proof_names(proof, "r", "l", "c" * 64, ["n" * 64, "m" * 64]) == ""
    assert "maintainers" in labs.proof_names(proof, "r", "l", "c" * 64, ["m" * 64])
    assert "creator" in labs.proof_names(proof, "r", "l", "d" * 64)
    assert labs.parse_proof(json.dumps({"type": labs.PROOF_TYPE, "relay": "r", "lab": "l", "creator": "c"}))[1]
    this_repo = labs.parse_proof((Path(__file__).resolve().parent.parent / ".emporia" / "lab.json").read_text())
    assert "FILL" in labs.proof_names(this_repo[0], "r", "emporia", "c" * 64)


def test_a_subject_is_a_github_repo_name_and_the_kind_is_open_but_unbuilt():
    assert labs.subject_problem({"kind": "github_repo", "ref": "acme/widgets"}) == ""
    assert "dataset" not in labs.SUBJECT_KINDS and "later" in labs.subject_problem({"kind": "dataset", "ref": "x/y"})
    assert labs.subject_problem({"kind": "github_repo", "ref": "https://github.com/a/b"})
    assert labs.check_lab("emporia", "n", {"kind": "github_repo", "ref": "a/b"}, "main", "0" * 40, ["m" * 64], "MIT") == ""


def test_credits_follow_the_operators_and_the_house_never_counts():
    op = {"m": "om", "a": "oa", "r": "or", "a2": "oa"}.get
    rows = [{"lab": "l", "ref": "gh_pr:a/b#1", "ref_kind": "gh_pr", "event": "merged", "maintainer": "m",
             "author": "a", "reviewers": ["r", "a2"], "at": "t"},
            {"lab": "l", "ref": "gh_issue:a/b#2", "ref_kind": "gh_issue", "event": "confirmed", "maintainer": "m",
             "author": "a", "reviewers": [], "at": "t"},
            {"lab": "l", "ref": "gh_pr:a/b#3", "ref_kind": "gh_pr", "event": "closed", "maintainer": "m",
             "author": "a", "reviewers": [], "at": "t"}]
    got = {(c["agent"], c["kind"]) for c in labs.credits(rows, lambda x: op(x, x))}
    assert got == {("a", "fix_landed"), ("r", "fix_reviewed"), ("a", "bug_confirmed")}, "a2 is the author's operator"
    assert not labs.credits(rows, lambda x: "same")
    assert not labs.credits(rows, lambda x: op(x, x), house={"a"})
    assert {c["agent"] for c in labs.credits(rows, lambda x: op(x, x), house={"r"})} == {"a"}
    assert labs.board(rows, lambda x: op(x, x))[0]["agent_id"] == "a"


# ── the relay ───────────────────────────────────────────────────────────────

def test_off_unless_turned_on_and_never_without_the_log(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_LABS", "0")
    assert client.get("/labs").status_code == 404
    monkeypatch.setenv("EMPORIA_LABS", "1")
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "off")
    r = client.get("/labs")
    assert r.status_code == 404 and "needs the log module" in r.json()["detail"]
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    assert client.get("/relay/card").json()["relay_modules"]["labs"]["parts"] == ["reports"]


def test_a_lab_is_made_only_when_the_repo_names_it_its_creator_and_its_maintainers(client, tmp_path, monkeypatch):
    lb = Lab(client, tmp_path, monkeypatch)
    other_room = rooms_db.create_room("someone else's", "someone", db_path=rs.DATABASE_PATH)
    assert post_lab(client, lb.creator, lb.slug, lb.repo, lb.commit, other_room.room_id,
                    [stable_of(lb.maint)], subject_ref=lb.subject).status_code == 422, "a room you made"
    # the proof names another maintainer than the signed terms
    r = post_lab(client, lb.creator, lb.slug, lb.repo, lb.commit, lb.room.room_id, [stable_of(lb.other)],
                 subject_ref=lb.subject)
    assert r.status_code == 422 and "maintainers" in r.json()["detail"]
    # another agent cannot claim the repo: the proof names the creator
    room2 = rooms_db.create_room("x", lb.other, db_path=rs.DATABASE_PATH)
    r = post_lab(client, lb.other, lb.slug, lb.repo, lb.commit, room2.room_id, [stable_of(lb.maint)],
                 subject_ref=lb.subject)
    assert r.status_code == 422 and "names creator" in r.json()["detail"]
    # a proof naming another relay
    lb2 = Lab(client, tmp_path, monkeypatch)
    lb2.repo, lb2.commit = make_repo(tmp_path, "x" + lb2.slug, stable_of(lb2.creator), [stable_of(lb2.maint)],
                                     relay="FILL_IN_THE_RELAY_ID")
    r = lb2.create()
    assert r.status_code == 422 and "operator fills it in" in r.json()["detail"]
    r = lb.create()
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["verified"] and out["kind"] == "lab" and out["subject"] == {"kind": "github_repo", "ref": lb.subject}
    assert out["maintainers"] == [stable_of(lb.maint)] and out["reports_are_public"]
    assert lb.create().status_code == 409, "one lab per repo"
    assert any(lb.slug in str(e) for e in [client.get("/labs").json()["labs"]])


def test_a_repo_unreadable_without_credentials_is_an_unverified_lab_only_the_operator_makes(client, tmp_path,
                                                                                         monkeypatch):
    lb = Lab(client, tmp_path, monkeypatch)
    gone = tmp_path / "private-repo-that-needs-a-login"
    r = post_lab(client, lb.other, lb.slug, gone, lb.commit, rooms_db.create_room("o", lb.other, db_path=rs.DATABASE_PATH).room_id,
                 [stable_of(lb.maint)], subject_ref=lb.subject, verified=False)
    assert r.status_code == 422
    r = post_lab(client, lb.creator, lb.slug, gone, lb.commit, lb.room.room_id, [stable_of(lb.maint)],
                 subject_ref=lb.subject, verified=False)
    assert r.status_code == 200, r.text
    assert r.json()["verified"] is False and "without credentials" in r.json()["unverified_note"]
    assert client.get(f"/labs/{lb.slug}").json()["verified"] is False


@pytest.mark.parametrize("room_kw", [{"room_type": "private", "gate_type": "invite"},
                                     {"room_type": "private", "gate_type": "paid", "entry_fee_cents": 500},
                                     {"room_type": "private", "gate_type": "invite", "encrypted": True}])
def test_a_lab_takes_the_rooms_own_options_and_says_what_its_room_is(client, tmp_path, monkeypatch, room_kw):
    lb = Lab(client, tmp_path, monkeypatch, room_kw)
    r = lb.create()
    assert r.status_code == 200, r.text
    room = r.json()["room"]
    assert room["type"] == "private" and room["gate"] == room_kw["gate_type"]
    assert room["encrypted"] == bool(room_kw.get("encrypted")) and room["entry_fee_cents"] == room_kw.get("entry_fee_cents", 0)
    assert r.json()["reports_are_public"] is True


def test_a_report_credits_the_author_and_a_reviewer_and_is_public_and_logged(client, lab):
    pr = lab.line(lab.author, "pr", 5)
    review = lab.line(lab.reviewer, "review", 5)
    r = lab.report(lab.maint, lab.pr(5), "merged", pr, [review], commit="a" * 40, note="thanks")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["author_id"] == lab.author and rep["reviewer_ids"] == [lab.reviewer] and rep["ref"] == lab.pr(5)
    got = client.get(f"/labs/{lab.slug}/reports").json()["reports"]
    assert [x["report_id"] for x in got] == [rep["report_id"]]
    assert client.get(f"/labs/{lab.slug}/reports", params={"ref": lab.pr(5)}).json()["reports"]
    assert client.get(f"/labs/{lab.slug}").json()["reports"] == 1
    with rs.DB_LOCK, rs.get_db() as conn:
        assert conn.execute("SELECT 1 FROM transparency_log WHERE kind = 'lab_report'").fetchone() if _has(conn, "transparency_log") else True
    assert "land-fix" in done(lab.author) and "lab-join" not in done(lab.author)
    assert "review-fix" in done(lab.reviewer) and "land-fix" not in done(lab.reviewer)
    board = client.get(f"/labs/{lab.slug}/board", headers=as_(lab.other)).json()["board"]
    assert {b["agent_id"]: b["fix_landed"] + b["fix_reviewed"] for b in board} == {lab.author: 1, lab.reviewer: 1}
    # an issue: confirmed, then fixed
    issue = lab.line(lab.author, "issue", 9)
    assert lab.report(lab.maint, lab.issue(9), "confirmed", issue).status_code == 200
    assert "lab-confirmed" in done(lab.author) and "lab-fixed" not in done(lab.author)
    assert lab.report(lab.maint, lab.issue(9), "fixed", issue).status_code == 200
    assert {"lab-confirmed", "lab-fixed"} <= done(lab.author)
    assert lab.report(lab.maint, lab.issue(9), "fixed", issue).status_code == 409, "once per ref and event"


def _has(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone())


def test_only_a_named_maintainer_reports_and_every_claim_is_checked(client, lab):
    ok = lab.line(lab.author, "pr", 1)
    assert lab.report(lab.author, lab.pr(1), "merged", ok).status_code == 403, "the author is not a maintainer"
    assert lab.report(lab.other, lab.pr(1), "merged", ok).status_code == 403
    # a forged claim: its signature is another agent's
    at = datetime.now(UTC).isoformat()
    claim = wire.signed_gh_claim_payload(lab.subject, "pr", 2, "", stable_of(lab.author), at)
    forged = labs.claim_line(f"{lab.author}@e2e", claim, signing.sign(lab.other, "gh-claim", lab.subject, "pr", 2, "",
                                                                       stable_of(lab.author), at))
    r = lab.report(lab.maint, lab.pr(2), "merged", forged)
    assert r.status_code == 422 and "does not verify" in r.json()["detail"]
    # a claim for another repo, another kind, another number
    other_repo = lab.line(lab.author, "pr", 3, repo="elsewhere/repo")
    assert "not this lab's" in lab.report(lab.maint, lab.pr(3), "merged", other_repo).json()["detail"]
    assert "claim is needed" in lab.report(lab.maint, lab.pr(4), "merged", lab.line(lab.author, "issue", 4)).json()["detail"]
    assert "not #5" in lab.report(lab.maint, lab.pr(5), "merged", lab.line(lab.author, "pr", 4)).json()["detail"]
    # a review claim cannot be the author's, and a ref in another repo is refused
    assert lab.report(lab.maint, lab.pr(6), "merged", lab.line(lab.author, "review", 6)).status_code == 422
    assert lab.report(lab.maint, "gh_pr:elsewhere/repo#6", "merged", lab.line(lab.author, "pr", 6)).status_code == 422
    # the wrong event for the ref; reviews only on a merge; a reviewer once, and not the author
    assert lab.report(lab.maint, lab.pr(7), "confirmed", lab.line(lab.author, "pr", 7)).status_code == 422
    assert lab.report(lab.maint, lab.issue(7), "confirmed", lab.line(lab.author, "issue", 7),
                      [lab.line(lab.reviewer, "review", 7)]).status_code == 422
    pr8, rv8 = lab.line(lab.author, "pr", 8), lab.line(lab.reviewer, "review", 8)
    assert lab.report(lab.maint, lab.pr(8), "merged", pr8, [rv8, rv8]).status_code == 422
    assert lab.report(lab.maint, lab.pr(8), "merged", pr8, [lab.line(lab.author, "review", 8)]).status_code == 422
    # a claim by an agent the relay does not know
    stranger = {"type": labs.CLAIM_TYPE, "repo": lab.subject, "kind": "pr", "number": 9, "head_commit": "",
                "agent": "f" * 64, "at": datetime.now(UTC).isoformat()}
    assert lab.report(lab.maint, lab.pr(9), "merged", labs.claim_line("x@y", stranger, "AAAA")).status_code == 422
    assert not client.get(f"/labs/{lab.slug}/reports").json()["reports"], "nothing above was kept"


def test_a_claim_is_checked_with_the_key_the_agent_had_when_it_signed(client, lab):
    from tests.test_emporia import _rotate_keyed
    old = lab.line(lab.author, "pr", 11)
    _rotate_keyed(client, lab.author)
    r = lab.report(lab.maint, lab.pr(11), "merged", old)
    assert r.status_code == 200, r.text
    # a claim dated before the rotation but signed by the new key does not verify
    when = (datetime.now(UTC) - __import__("datetime").timedelta(hours=1)).isoformat()
    backdated = lab.line(lab.author, "pr", 12, at=when)
    assert lab.report(lab.maint, lab.pr(12), "merged", backdated).status_code == 422


def test_nothing_counts_between_one_operator_or_for_the_house(client, lab, monkeypatch):
    # the maintainer reports its own operator's PR: no credit, no board row
    same_operator(lab.maint, lab.author)
    assert lab.report(lab.maint, lab.pr(20), "merged", lab.line(lab.author, "pr", 20)).status_code == 200
    assert lab.report(lab.maint, lab.issue(21), "confirmed", lab.line(lab.author, "issue", 21)).status_code == 200
    assert not done(lab.author) & {"land-fix", "lab-confirmed"}
    assert client.get(f"/labs/{lab.slug}/board", headers=as_(lab.other)).json()["board"] == []
    # a reviewer of the author's operator is not credited; another operator's is
    same_operator(lab.reviewer, lab.author)
    same_operator(lab.maint)         # the maintainer is its own operator again
    assert lab.report(lab.maint, lab.pr(22), "merged", lab.line(lab.author, "pr", 22),
                      [lab.line(lab.reviewer, "review", 22), lab.line(lab.other, "review", 22)]).status_code == 200
    assert "review-fix" not in done(lab.reviewer) and "review-fix" in done(lab.other)
    assert "land-fix" in done(lab.author)
    # the house never counts: not as the maintainer whose report it is, nor as the author
    monkeypatch.setenv("EMPORIA_HOUSE_AGENTS", lab.maint)
    assert "land-fix" not in done(lab.author)
    assert client.get(f"/labs/{lab.slug}/board", headers=as_(lab.other)).json()["board"] == []
    monkeypatch.setenv("EMPORIA_HOUSE_AGENTS", lab.author)
    board = client.get(f"/labs/{lab.slug}/board", headers=as_(lab.other)).json()["board"]
    assert lab.author not in {b["agent_id"] for b in board}


def test_a_lab_in_every_relay_counts_not_just_the_first(client, lab, tmp_path, monkeypatch):
    second = Lab(client, tmp_path, monkeypatch)
    assert second.create().status_code == 200
    one = second.line(lab.author, "pr", 30)
    second.slug  # the author is a stranger to the second lab's operators too
    r = second.report(second.maint, second.pr(30), "merged", one)
    assert r.status_code == 200, r.text
    assert "land-fix" in done(lab.author)


def test_a_private_labs_threads_and_board_are_for_members_but_its_reports_still_count(client, tmp_path, monkeypatch):
    lb = Lab(client, tmp_path, monkeypatch, {"room_type": "private", "gate_type": "invite"})
    assert lb.create().status_code == 200
    rooms_db.add_member(lb.room.room_id, lb.maint, db_path=rs.DATABASE_PATH)
    rooms_db.post_message(room_id=lb.room.room_id, sender_id=lb.maint, content="looking at it", msg_type="chat",
                          db_path=rs.DATABASE_PATH, reply_to=lb.pr(40), root=lb.pr(40))
    for path in ("threads", "board"):
        assert client.get(f"/labs/{lb.slug}/{path}", headers=as_(lb.other)).status_code == 403
        assert client.get(f"/labs/{lb.slug}/{path}").status_code == 403
        assert client.get(f"/labs/{lb.slug}/{path}", headers=as_(lb.maint)).status_code == 200
    assert client.get("/thread", params={"ref": lb.pr(40)}, headers=as_(lb.other)).status_code == 403
    r = lb.report(lb.maint, lb.pr(40), "merged", lb.line(lb.author, "pr", 40))
    assert r.status_code == 200, r.text
    # public to anyone, and it still credits the quest
    assert [x["ref"] for x in client.get(f"/labs/{lb.slug}/reports").json()["reports"]] == [lb.pr(40)]
    assert "land-fix" in done(lb.author)
    # an encrypted lab: the same
    enc = Lab(client, tmp_path, monkeypatch, {"room_type": "private", "gate_type": "invite", "encrypted": True})
    assert enc.create().status_code == 200
    assert enc.report(enc.maint, enc.issue(41), "confirmed", enc.line(enc.author, "issue", 41)).status_code == 200
    assert client.get(f"/labs/{enc.slug}/reports").status_code == 200 and "lab-confirmed" in done(enc.author)
    assert client.get(f"/labs/{enc.slug}/threads", headers=as_(enc.other)).status_code == 403


def test_threads_are_rooted_at_an_issue_or_pr_and_show_their_latest_state(client, lab):
    for n, who in ((50, lab.author), (51, lab.reviewer)):
        rooms_db.post_message(room_id=lab.room.room_id, sender_id=who, content=f"on #{n}", msg_type="chat",
                              db_path=rs.DATABASE_PATH, reply_to=lab.pr(n), root=lab.pr(n))
    rooms_db.post_message(room_id=lab.room.room_id, sender_id=lab.author, content="the issue", msg_type="chat",
                          db_path=rs.DATABASE_PATH, reply_to=lab.issue(52), root=lab.issue(52))
    lab.report(lab.maint, lab.pr(50), "merged", lab.line(lab.author, "pr", 50))
    lab.report(lab.maint, lab.issue(52), "confirmed", lab.line(lab.author, "issue", 52))
    threads = {t["ref"]: t for t in client.get(f"/labs/{lab.slug}/threads").json()["threads"]}
    assert threads[lab.pr(50)]["state"] == "merged" and threads[lab.pr(51)]["state"] == "open"
    assert threads[lab.issue(52)]["state"] == "confirmed"
    got = client.get("/thread", params={"ref": lab.pr(51)}).json()
    assert [p["text"] for p in got["posts"]] == ["on #51"] or got["count"] >= 1
    skim = client.get("/thread", params={"ref": lab.pr(50), "mode": "skim"}).json()
    assert skim["mode"] == "skim" and skim["stats"]["items"] >= 1
    assert client.get("/thread", params={"ref": "gh_pr:nobody/none#1"}).status_code == 404


def test_the_lab_join_quest_is_a_message_in_a_labs_room(client, lab):
    from emporia.routes import standing
    rooms_db.post_message(room_id=lab.room.room_id, sender_id=lab.other, content="hello", msg_type="chat",
                          db_path=rs.DATABASE_PATH)
    got = standing._lab_findings(lab.other)
    assert [f["creator"] for f in got] == [lab.creator]
    assert "lab-join" in done(lab.other)
    assert "lab" not in {a["id"] for a in standing.quests_body(lab.author)["arcs"] if a["complete"]}


def test_the_kinds_are_signed_through_the_one_door_and_logged():
    assert {"lab", "gh-claim", "lab-report"} <= set(signing.KINDS)
    assert "lab-patch" not in signing.KINDS and "lab-check" not in signing.KINDS
    assert {"lab", "lab_report"} <= set(translog.SOURCES) and "lab_patch" not in translog.SOURCES


# ── the tools, the key guard, doctor, the CLI ───────────────────────────────

def test_the_tools_make_and_read_a_claim_line(monkeypatch, client):
    from emporia import mcp_server
    a = name("tool")
    _register_keyed(client, a)
    monkeypatch.setattr(mcp_server, "AGENT_ID", a)
    monkeypatch.setattr(mcp_server, "_signing_identity", lambda aid: stable_of(a))
    monkeypatch.setattr(mcp_server.httpx, "get", lambda *x, **k: type("R", (), {
        "status_code": 200, "content": b"{}", "json": lambda self: {"relay_id": "e2e"}})())
    fn = getattr(mcp_server.github_claim_line, "fn", mcp_server.github_claim_line)
    out = fn(REPO_NAME, "pr", 3)
    assert out["success"] and out["line"].startswith(f"Emporia: {a}@e2e ")
    claim, _sig, why = labs.decode_claim(out["line"])
    assert not why and claim["agent"] == stable_of(a) and claim["number"] == 3
    assert not fn("https://github.com/a/b", "pr", 3)["success"] and not fn(REPO_NAME, "merge", 3)["success"]
    assert not hasattr(mcp_server, "submit_patch") and not hasattr(mcp_server, "patch_status")


def test_a_relay_started_turn_never_gets_gh_or_the_terminal(monkeypatch):
    from hermes_plugin.emporia import relay_turn
    monkeypatch.delenv("EMPORIA_RELAY_TURN_TOOLS", raising=False)
    sid = f"s-{uuid.uuid4().hex}"
    relay_turn.pre_llm_call(platform="emporia", session_id=sid)
    for tool in ("terminal", "execute_code", "github", "github_create_pull_request", "web_extract", "write_file"):
        v = relay_turn.pre_tool_call(tool_name=tool, session_id=sid)
        assert v and v["action"] == "block", tool
    assert relay_turn.pre_tool_call(tool_name="mcp_emporia_lab_report", session_id=sid) is None
    mine = f"s-{uuid.uuid4().hex}"
    relay_turn.pre_llm_call(platform="cli", session_id=mine)
    assert relay_turn.pre_tool_call(tool_name="terminal", session_id=mine) is None, "a person's own turn"


def test_the_cli_takes_either_order_and_create_is_a_persons(monkeypatch, capsys):
    from emporia import cli
    agents = {"opdesk": Path("/x")}
    assert cli._lab_words(["opdesk", "show", "emporia"], agents) == ("opdesk", "show", ["emporia"])
    assert cli._lab_words(["opdesk"], agents) == ("opdesk", "list", [])
    monkeypatch.setattr(cli, "_agent_profiles", lambda root: [(Path("/x/opdesk"), {})])
    monkeypatch.setattr(cli, "_agent", lambda n: (Path("/x/opdesk"), "opdesk", {"EMPORIA_AGENT_ID": "opdesk"}))
    monkeypatch.setattr(cli, "_persons_terminal", lambda: False)
    args = cli.build_parser().parse_args(["lab", "opdesk", "create", "emporia", "--repo", "https://github.com/a/b",
                                          "--room-type", "private", "--gate", "paid", "--entry-fee-cents", "100",
                                          "--encrypted"])
    assert args.room_type == "private" and args.gate == "paid" and args.encrypted and args.entry_fee_cents == 100
    assert args.func(args) == 1 and "only a person" in capsys.readouterr().out
    for gone in ("check", "land", "reject"):
        assert cli.build_parser().parse_args(["lab", "opdesk", "list"]) and gone not in cli.LAB_ACTS


def test_the_operator_desk_shows_lab_activity_not_patches(client, lab):
    from emporia import operator_desk
    lab.report(lab.maint, lab.pr(60), "merged", lab.line(lab.author, "pr", 60))
    rooms_db.post_message(room_id=lab.room.room_id, sender_id=lab.author, content="x", msg_type="chat",
                          db_path=rs.DATABASE_PATH, reply_to=lab.issue(61), root=lab.issue(61))
    from emporia.routes import operator as op_routes
    with rs.DB_LOCK, rs.get_db() as conn:
        act = op_routes.lab_activity(conn)
    assert act["on"] and act["reports"] >= 1
    assert [r["event"] for r in act["recent"] if r["lab"] == lab.slug] == ["merged"]
    assert [t["ref"] for t in act["open_threads"] if t["lab"] == lab.slug] == [lab.issue(61)]
    text = "\n".join(operator_desk.render({"labs": act, "agent_id": "x"}, "opdesk"))
    assert "Lab activity" in text and lab.issue(61) in text and "hermes emporia lab opdesk show" in text
    assert "Patches" not in text


# ── the optional public check, with a fake `gh` ──────────────────────────────

def fake_gh(tmp_path, monkeypatch, pulls, issues):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text("#!/usr/bin/env python3\nimport json, sys\n"
                  f"pulls, issues = {pulls!r}, {issues!r}\n"
                  "path = sys.argv[2]\n"
                  "d = (pulls if '/pulls/' in path else issues).get(path)\n"
                  "if d is None:\n    sys.stderr.write('HTTP 404: Not Found')\n    sys.exit(1)\n"
                  "print(json.dumps(d))\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def test_verify_lab_report_checks_the_public_repo_with_gh(tmp_path, monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("verify_lab_report", Path(__file__).resolve().parent.parent
                                                  / "scripts" / "verify_lab_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sha = "c" * 40
    fake_gh(tmp_path, monkeypatch, {"repos/a/b/pulls/1": {"merged": True, "merge_commit_sha": sha, "state": "closed"},
                                    "repos/a/b/pulls/2": {"merged": False, "state": "open"}},
            {"repos/a/b/issues/3": {"state": "closed"}})
    reports = [{"ref": "gh_pr:a/b#1", "event": "merged", "commit": sha},
               {"ref": "gh_issue:a/b#3", "event": "fixed", "commit": ""}]
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"reports": reports}))
    assert mod.main([str(f)]) == 0
    assert capsys.readouterr().out.count("OK") == 2
    f.write_text(json.dumps({"reports": [{"ref": "gh_pr:a/b#1", "event": "merged", "commit": "d" * 40},
                                         {"ref": "gh_pr:a/b#2", "event": "merged", "commit": ""},
                                         {"ref": "gh_pr:a/b#9", "event": "merged", "commit": ""}]}))
    assert mod.main([str(f)]) == 1
    out = capsys.readouterr().out
    assert out.count("MISMATCH") == 3 and "not the reported" in out and "not merged" in out and "cannot be read" in out
    monkeypatch.setenv("PATH", "/nonexistent")
    assert mod.main([str(f)]) == 0 and "nothing was checked" in capsys.readouterr().out


# ── doctor's Labs section and the setup line ────────────────────────────────

def fake_gh_state(tmp_path, monkeypatch, state):
    """A `gh` on PATH whose `auth status` says `state`: in (signed in), scopes (a broad token), out."""
    bindir = tmp_path / "ghbin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    gh.write_text("#!/bin/sh\n"
                  'if [ "$1" = "--version" ]; then echo "gh version 2.0.0"; exit 0; fi\n'
                  f'case "{state}" in\n'
                  '  in) echo "Logged in to github.com as someone"; exit 0;;\n'
                  '  scopes) echo "Logged in to github.com as someone (Token scopes: repo, workflow)"; exit 0;;\n'
                  '  *) echo "You are not logged into any GitHub hosts" >&2; exit 1;;\nesac\n')
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def labs_report(capsys):
    from emporia import cli
    rep = cli.Report(compact=False)
    cli.doctor_labs(rep)
    rep.flush()
    return capsys.readouterr().out, rep.failed


def test_setup_ends_with_one_line_about_labs(capsys):
    from hermes_plugin.emporia import setup_cli
    setup_cli._labs_line("someone")
    out = capsys.readouterr().out
    assert "emporia doctor" in out and "Labs section" in out and "gh" in out
    assert len(out.strip().splitlines()) <= 2


# ── a GitHub token never reaches the model, a thread or a report ─────────────

TOKENS = ["ghp_" + "A1b2" * 9, "gho_" + "x" * 36, "ghu_" + "Y" * 36, "ghs_" + "z9" * 18, "ghr_" + "Q" * 36,
          "github_pat_" + "11ABC" * 12]


def test_nothing_that_looks_like_a_github_token_is_sent(monkeypatch, client):
    import asyncio

    from emporia import mcp_server, secret_guard
    from emporia.engine import guardrails
    a = name("leak")
    _register_keyed(client, a)
    for tok in TOKENS:
        for body in ({"text": f"here {tok}"}, {"label": tok}, {"tags": [["t", tok]]}, {"note": tok}):
            with pytest.raises(secret_guard.SecretLeak, match="GitHub token"):
                secret_guard.check(a, body, None)
        with pytest.raises(guardrails.GuardrailBlocked):
            guardrails.assert_payload_safe({"text": f"see {tok}"})
        with pytest.raises(PermissionError):
            asyncio.run(rs._assert_payload_safe_counted({"text": f"see {tok}", "label": "", "tags": []}))
    assert guardrails.assert_payload_safe({"text": "ghp_short is not a token"}).ok
    monkeypatch.setattr(mcp_server, "AGENT_ID", a)
    with pytest.raises(secret_guard.SecretLeak):
        mcp_server._bearer_request("post", "http://127.0.0.1:1/labs/x/reports", agent_id=a,
                                   json={"note": f"ok {TOKENS[0]}"})     # lab_report, and every other write
    fn = getattr(mcp_server.github_claim_line, "fn", mcp_server.github_claim_line)
    out = fn(TOKENS[0] + "/widgets", "pr", 1)
    assert not out["success"] and "credential" in out["error"]




def test_the_key_guard_protects_emporias_keys_not_github_ones():
    from hermes_plugin.emporia import key_guard as guard
    for cmd in ("echo $GITHUB_TOKEN", "gh auth token", "cat ~/.config/gh/hosts.yml", "gh pr create --fill",
                "git push origin feature"):
        assert guard.pre_tool_call("terminal", {"command": cmd}) is None, cmd     # Hermes's concern, not Emporia's
    for cmd in ("cat ~/.hermes/keys/agent.priv", "emporia lab opdesk create emporia --repo x"):
        v = guard.pre_tool_call("terminal", {"command": cmd})
        assert v and v["action"] == "block", cmd
    assert guard.transform_tool_result("token ghp_" + "A" * 36) is None, "redacting tokens is Hermes's job"


def test_doctor_labs_is_friendly_information_in_each_state_and_never_a_failure(tmp_path, monkeypatch, capsys):
    fake_gh_state(tmp_path, monkeypatch, "in")
    out, failed = labs_report(capsys)
    assert "Labs: discussing and helping needs nothing extra" in out and "`gh` is on PATH" in out
    assert "gh shows a signed-in account" in out and "someone" not in out, "no account name, never a token"
    assert "README" in out and not failed
    fake_gh_state(tmp_path, monkeypatch, "out")
    out, failed = labs_report(capsys)
    assert "gh is not signed in" in out and "gh auth login" in out and not failed
    monkeypatch.setenv("PATH", "/nonexistent")
    out, failed = labs_report(capsys)
    assert "`git` is not on PATH" in out and "`gh` is not on PATH" in out and "install it yourself" in out
    assert not failed
    from emporia import cli
    assert not hasattr(cli, "doctor_no_github_token") and not hasattr(cli, "github_scope_findings")


def test_the_outbound_block_covers_the_common_credential_shapes_at_the_relay_too():
    import asyncio

    from emporia.engine import guardrails
    for text in ("sk-ant-" + "a" * 30, "AKIA" + "ABCDEFGHIJKLMNOP", "sk_live_" + "a1" * 12,
                 "-----BEGIN " + "PRIVATE KEY-----", "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + "." + "c" * 12):
        with pytest.raises(guardrails.GuardrailBlocked, match="credential"):
            guardrails.assert_payload_safe({"text": f"here {text}"})
        assert guardrails.scan_text(f"here {text}", mode="enforce").ok, "received text is not flagged as injection"
        with pytest.raises(PermissionError):
            asyncio.run(rs._assert_payload_safe_counted({"text": text, "label": "", "tags": []}))
    assert guardrails.assert_payload_safe({"text": "a normal sentence about a PR and a tag"}).ok


# ── the protocol freeze: stable ids, content-hash ids, a pinned operator ────

def test_an_unverified_lab_needs_a_pinned_operator_and_fails_closed(client, tmp_path, monkeypatch):
    lb = Lab(client, tmp_path, monkeypatch)
    gone = tmp_path / "private-repo-without-a-login"
    args = (lb.creator, lb.slug, gone, lb.commit, lb.room.room_id, [stable_of(lb.maint)])
    monkeypatch.delenv("EMPORIA_OPERATOR_STABLE_ID")
    r = post_lab(client, *args, subject_ref=lb.subject, verified=False)
    assert r.status_code == 422, "no operator pinned: nobody is the operator"
    monkeypatch.setenv("EMPORIA_OPERATOR_STABLE_ID", stable_of(lb.other))
    assert post_lab(client, *args, subject_ref=lb.subject, verified=False).status_code == 422
    monkeypatch.setenv("EMPORIA_OPERATOR_STABLE_ID", stable_of(lb.creator))
    assert post_lab(client, *args, subject_ref=lb.subject, verified=False).status_code == 200


def test_the_terms_and_a_report_are_signed_by_stable_id_and_the_relays_note_has_a_hash_id(client, lab):
    row = client.get(f"/labs/{lab.slug}").json()
    assert row["creator"] == stable_of(lab.creator) and "maintainer_ids" not in row
    payload = wire.signed_lab_payload(lab.slug, lab.slug, lab.room.room_id, row["subject"], "main", lab.commit,
                                      [stable_of(lab.maint)], "MIT", stable_of(lab.creator), row["at"])
    assert payload["stable_id"] == stable_of(lab.creator) and "agent_id" not in payload
    assert rs.AGENT_REGISTRY.verify_signature(lab.creator, payload, row["signature"])
    r = lab.report(lab.maint, lab.pr(60), "merged", lab.line(lab.author, "pr", 60),
                   [lab.line(lab.reviewer, "review", 60)])
    assert r.status_code == 200, r.text
    assert r.json()["author_stable_id"] == stable_of(lab.author)
    assert r.json()["reviewer_stable_ids"] == [stable_of(lab.reviewer)]
    msgs = rooms_db.get_messages(lab.room.room_id, db_path=rs.DATABASE_PATH)
    assert msgs and all(m.message_id.startswith("msg_") for m in msgs), [m.message_id for m in msgs]
    note = next(m for m in msgs if m.root == lab.pr(60))
    assert note.reply_to == lab.pr(60) and note.root == lab.pr(60)
    board = client.get(f"/labs/{lab.slug}/board", headers=as_(lab.other)).json()["board"]
    assert board and all(b["stable_id"] for b in board)


def test_a_member_replies_in_a_lab_thread_with_a_signed_root(client, lab):
    from tests.test_rooms_v2 import _say
    rooms_db.add_member(lab.room.room_id, lab.author, db_path=rs.DATABASE_PATH)
    good = _say(client, lab.room.room_id, lab.author, "on it", reply_to=lab.pr(61))
    assert good.status_code == 200, good.text
    assert good.json()["message_id"].startswith("msg_") and good.json()["root"] == lab.pr(61)
    bad = _say(client, lab.room.room_id, lab.author, "elsewhere", reply_to=lab.pr(61),
               content=__import__("emporia.room_post", fromlist=["x"]).canonical(
                   __import__("emporia.room_post", fromlist=["x"]).body("note", "elsewhere", [], lab.pr(61),
                                                                         root=lab.pr(62))))
    assert bad.status_code == 400
