"""Adopted ideas: improvement points, credited by the labs' reports.

There is no operator verdict. A lab maintainer's signed report on a merged PR or a fixed issue names the
idea posts it implemented (`emporia:lab-report:v2`, `ideas`); the relay credits each idea once, to its author,
when the author's operator is not the maintainer's and never to the house. Points, the sheet, the season's
`ideas` board and the quest all read those reports."""
import pytest
from fastapi.testclient import TestClient

from emporia import ideas, labs, progression, signing, translog, wire
from emporia import relay_server as rs
from tests.test_emporia import _register_keyed
from tests.test_labs import Lab, as_, done, name, same_operator
from tests.topic_posts import seed_post_row


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EMPORIA_LABS", "1")
    monkeypatch.setenv("EMPORIA_LAB_HOSTS", "file")
    monkeypatch.setenv("EMPORIA_QUEST_OTHERS", "any")
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    monkeypatch.delenv("EMPORIA_HOUSE_AGENTS", raising=False)
    return TestClient(rs.app, client=("127.0.0.1", 51312))


@pytest.fixture
def lab(client, tmp_path, monkeypatch):
    """A lab on a relay with sheets on, for the ideas' points, board and quest."""
    monkeypatch.delenv("EMPORIA_IDEA_POINTS", raising=False)
    lb = Lab(client, tmp_path, monkeypatch)
    assert lb.create().status_code == 200
    return lb


def _topic(slug="ideas", visibility="public"):
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO agora_topics (topic_id, slug, name, visibility, creator_id, created_at) "
                     "VALUES (?, ?, ?, ?, 'op', '2026-11-01T00:00:00+00:00')", (f"t_{slug}", slug, slug, visibility))
    return slug


def idea(author, kind="idea", topic="ideas", visibility="public", at="2026-11-05T00:00:00+00:00"):
    """A post row; returns its ref text, `post:pst_…`."""
    text = "Show the claimed severity\nbecause"
    return "post:" + seed_post_row(author, _topic(topic, visibility), text, at, kind=kind)


def merged(lb, n, *named, author=None):
    """The maintainer reports PR #n merged, naming these ideas."""
    return lb.report(lb.maint, lb.pr(n), "merged", lb.line(author or lb.author, "pr", n), ideas=named)


def points(client, agent):
    return client.get(f"/agents/{agent}/points").json()


# ── the rules ──────────────────────────────────────────────────────────────

def test_points_are_configurable_and_rows_come_from_the_credits(monkeypatch):
    monkeypatch.delenv("EMPORIA_IDEA_POINTS", raising=False)
    assert ideas.points() == 10 == ideas.DEFAULT_POINTS
    monkeypatch.setenv("EMPORIA_IDEA_POINTS", "25")
    credits = [{"kind": "idea_adopted", "agent": "a", "idea": "pst_1", "at": "t", "lab": "l", "ref": "gh_pr:a/b#1"},
               {"kind": "fix_landed", "agent": "b"}]
    rows = ideas.adopted(credits)
    assert rows == [{"post_id": "pst_1", "author_id": "a", "points": 25, "at": "t", "lab": "l", "ref": "gh_pr:a/b#1"}]
    assert ideas.points_of("a", rows)["points"] == 25 and ideas.points_of("a", rows)["badges"] == ["Idea adopted"]
    assert ideas.points_of("b", rows) == {"agent_id": "b", "points": 0, "adopted": 0, "badges": []}
    monkeypatch.setenv("EMPORIA_IDEA_POINTS", "lots")
    assert ideas.points() == 10, "a value that is not a number is the default, not zero"


def test_the_report_names_its_ideas_in_what_is_signed_and_there_is_no_verdict_kind():
    p = wire.signed_lab_report_payload("lab", "gh_pr:a/b#1", "merged", "", "blob", [], ["post:pst_1"], "", "sid", "t")
    assert p["type"] == "emporia:lab-report:v2" and p["ideas"] == ["post:pst_1"]
    assert "idea-verdict" not in signing.KINDS and not hasattr(wire, "signed_idea_verdict_payload")
    assert labs.REPORT_TYPE == "emporia:lab-report:v2"


def test_credits_name_an_idea_once_for_its_author_across_operators_only():
    op = {"m": "om", "a": "oa", "i": "oi", "mine": "om"}.get
    row = {"lab": "l", "ref": "gh_pr:a/b#1", "ref_kind": "gh_pr", "event": "merged", "maintainer": "m",
           "author": "a", "reviewers": [], "at": "t",
           "ideas": [{"post": "p1", "author": "i"}, {"post": "p2", "author": "mine"}, {"post": "p3", "author": "h"},
                     {"post": "p4", "author": ""}]}
    got = [(c["agent"], c["idea"]) for c in labs.credits([row], lambda x: op(x, x), house={"h"}) if c["kind"] == "idea_adopted"]
    assert got == [("i", "p1")], "not the maintainer's own operator, not the house, not a post that is gone"
    again = dict(row, ref="gh_pr:a/b#2")
    assert len([c for c in labs.credits([row, again], lambda x: op(x, x)) if c["agent"] == "i"]) == 1, "once"
    closed = dict(row, event="closed")
    assert not [c for c in labs.credits([closed], lambda x: op(x, x)) if c["kind"] == "idea_adopted"]
    fixed = {**row, "ref_kind": "gh_issue", "event": "fixed"}
    assert [c["agent"] for c in labs.credits([fixed], lambda x: op(x, x), house={"h"}) if c["kind"] == "idea_adopted"] == ["i"]
    assert labs.credits([row], lambda x: "same") == [], "one operator throughout: nothing"
    # the idea's credit does not depend on who wrote the work (here the PR's author shares the maintainer's operator)
    own_work = dict(row, author="mine")
    assert [c["agent"] for c in labs.credits([own_work], lambda x: op(x, x), house={"h"})] == ["i"]


def test_the_board_counts_ideas_in_the_season_and_never_the_house():
    s = {"season": "s", "started_at": "2026-11-01T00:00:00+00:00", "ends_at": None}
    out = progression.season_boards([], [], None, 0.0, s, frozenset({"house"}), 25, ideas=[
        {"author_id": "a", "points": 10, "at": "2026-11-05T00:00:00+00:00"},
        {"author_id": "a", "points": 10, "at": "2026-10-05T00:00:00+00:00"},
        {"author_id": "house", "points": 10, "at": "2026-11-05T00:00:00+00:00"}])
    assert out["boards"]["ideas"] == [{"agent_id": "a", "points": 10, "adopted": 1}]


# ── the relay ──────────────────────────────────────────────────────────────

def test_a_cross_operator_idea_in_a_merged_report_credits_its_author(client, lab, monkeypatch):
    monkeypatch.setenv("EMPORIA_IDEA_POINTS", "12")
    thinker = lab.other
    ref = idea(thinker)
    r = merged(lab, 5, ref)
    assert r.status_code == 200, r.text
    assert r.json()["ideas"] == [ref]
    pts = points(client, thinker)
    assert (pts["points"], pts["adopted"]) == (12, 1) and "Idea adopted" in pts["badges"]
    assert points(client, lab.author)["points"] == 0, "the PR's author earns the fix, not the idea"
    board = {r["agent_id"]: r for r in client.get("/season").json()["boards"]["ideas"]}
    assert board[thinker] == {"agent_id": thinker, "points": 12, "adopted": 1}
    sheet = client.get(f"/agents/{thinker}/sheet").json()
    assert sheet["improvements"]["points"] == 12 and "Improve  12 points" in progression.render(sheet)
    assert "idea-adopted" in done(thinker) and "land-fix" in done(lab.author)
    labboard = {b["agent_id"]: b for b in client.get(f"/labs/{lab.slug}/board", headers=as_(lab.reviewer)).json()["board"]}
    assert labboard[thinker]["idea_adopted"] == 1
    listed = client.get(f"/labs/{lab.slug}/reports").json()["reports"]
    assert listed[0]["ideas"] == [ref], "public with the report"
    with rs.DB_LOCK, rs.get_db() as conn:
        translog.fold_all(conn)
        rec = [e["record"] for e in translog.entries(conn, 0, ref=f"lab_report:{r.json()['report_id']}", limit=10)]
    assert rec and "ideas_json" in str(rec), "the log carries the ideas"


def test_a_fixed_issue_may_name_ideas_too_and_a_closed_one_may_not(client, lab):
    ref = idea(lab.other)
    bad = lab.report(lab.maint, lab.issue(3), "confirmed", lab.line(lab.author, "issue", 3), ideas=[ref])
    assert bad.status_code == 422 and "merged pull request or a fixed issue" in bad.json()["detail"]
    shut = lab.report(lab.maint, lab.pr(4), "closed", lab.line(lab.author, "pr", 4), ideas=[ref])
    assert shut.status_code == 422
    ok = lab.report(lab.maint, lab.issue(3), "fixed", lab.line(lab.author, "issue", 3), ideas=[ref])
    assert ok.status_code == 200, ok.text
    assert points(client=lab.client, agent=lab.other)["points"] == 10


def test_same_operator_and_house_ideas_are_recorded_and_earn_nothing(client, lab, monkeypatch):
    mine, house = name("mine"), name("house")
    for a in (mine, house):
        _register_keyed(client, a)
    same_operator(lab.maint, mine)
    monkeypatch.setenv("EMPORIA_HOUSE_AGENTS", house)
    own, hs = idea(mine), idea(house)
    r = merged(lab, 6, own, hs)
    assert r.status_code == 200, r.text
    assert r.json()["ideas"] == [own, hs], "recorded in the report"
    assert points(client, mine)["points"] == 0 and points(client, house)["points"] == 0
    on_board = {r["agent_id"] for r in client.get("/season").json()["boards"]["ideas"]}
    assert not on_board & {mine, house}
    assert "idea-adopted" not in done(mine)
    # recorded means spent: naming the same idea in another report is refused
    again = merged(lab, 7, own)
    assert again.status_code == 409


def test_only_a_live_public_idea_of_this_relay_is_named(client, lab):
    note = idea(lab.other, kind="note")
    r = merged(lab, 8, note)
    assert r.status_code == 422 and "not an idea" in r.json()["detail"]
    private = idea(lab.other, topic="inner-circle", visibility="private")
    r = merged(lab, 8, private)
    assert r.status_code == 422 and "not a public post" in r.json()["detail"]
    r = merged(lab, 8, "post:pst_" + "0" * 28)
    assert r.status_code == 422 and "no post" in r.json()["detail"]
    gone = idea(lab.other)
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE posts SET is_deleted = 1 WHERE post_id = ?", (gone.split(":")[1],))
        conn.commit()
    assert merged(lab, 8, gone).status_code == 422
    for bad in ("pst_x", "post:pst_x@other.example", "gh_pr:a/b#1", "not a ref"):
        assert merged(lab, 8, bad).status_code == 422, bad
    reply = seed_post_row(lab.other, _topic(), "a reply", "2026-11-05T00:00:00+00:00", kind="idea", reply_to="pst_x")
    assert merged(lab, 8, f"post:{reply}").status_code == 422, "a top-level post only"
    assert points(client, lab.other)["points"] == 0
    # none of those refused reports was recorded: PR 8 is still open
    assert client.get(f"/labs/{lab.slug}/reports", params={"ref": lab.pr(8)}).json()["reports"] == []


def test_an_idea_is_credited_once_and_at_most_five_per_report(client, lab):
    ref = idea(lab.other)
    assert merged(lab, 9, ref).status_code == 200
    dup = merged(lab, 10, ref)
    assert dup.status_code == 409 and "adopted once" in dup.json()["detail"]
    assert client.get(f"/labs/{lab.slug}/reports", params={"ref": lab.pr(10)}).json()["reports"] == []
    assert points(client, lab.other) ["points"] == 10, "still one credit"
    six = [idea(lab.other) for _ in range(6)]
    assert merged(lab, 11, *six).status_code == 422
    twice = idea(lab.other)
    assert merged(lab, 11, twice, twice).status_code == 422
    assert merged(lab, 11, *six[:5]).status_code == 200
    assert points(client, lab.other)["points"] == 60


def test_the_operators_verdict_is_gone(client, lab):
    pid = idea(lab.other).split(":")[1]
    assert client.post(f"/ideas/{pid}/verdict", headers=as_(lab.creator), json={"verdict": "adopted"}).status_code in (404, 405)
    assert client.get("/ideas").status_code == 404
    status = client.get("/operator/status", headers=as_(lab.creator))
    assert status.status_code == 200 and "ideas" not in status.json()
    from emporia import cli, operator_desk, relay_modules
    assert not hasattr(cli, "cmd_ideas") and not hasattr(operator_desk, "idea_commands")
    assert "ideas" not in relay_modules.GAMIFY_PARTS


def test_points_follow_the_labs_module(client, lab, monkeypatch):
    monkeypatch.setenv("EMPORIA_LABS", "0")
    assert client.get(f"/agents/{lab.other}/points").status_code == 404
    monkeypatch.setenv("EMPORIA_LABS", "1")
    assert client.get(f"/agents/{lab.other}/points").status_code == 200
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,ideas")
    from emporia import relay_modules
    assert "no part called ideas" in " ".join(relay_modules.problems()), "an old setting is refused loudly"
