"""Season 1: adopted ideas, "called it", and the improvement board.

The operator's signed verdict on a post in the ideas topic, reusing the bug verdict's
mechanism (operator only, final, logged); a reporter's claimed severity beside the
awarded one; adopted ideas' points on the season's bug board and the sheet."""
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from emporia import bugs, ideas, progression, signing, translog, wire
from emporia import relay_server as rs
from tests.test_bugs import file
from tests.test_emporia import _register_keyed


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,bugs")
    monkeypatch.delenv("EMPORIA_IDEA_POINTS", raising=False)
    monkeypatch.delenv("EMPORIA_HOUSE_AGENTS", raising=False)
    return TestClient(rs.app, client=("127.0.0.1", 51297))


def as_(agent):
    return {"x-emporia-agent-id": agent}


def name(tag):
    return f"id_{tag}_{uuid.uuid4().hex[:6]}"


def _ideas_topic():
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO agora_topics (topic_id, slug, name, visibility, creator_id, created_at) "
                     "VALUES ('t_ideas', 'ideas', 'ideas', 'public', 'op', '2026-11-01T00:00:00+00:00')")
        return conn.execute("SELECT topic_id FROM agora_topics WHERE slug = 'ideas'").fetchone()[0]


def idea(author, title="Show the claimed severity", at="2026-11-05T00:00:00+00:00", topic_id=None):
    pid = f"p_{uuid.uuid4().hex[:8]}"
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO agora_posts (post_id, topic_id, author_id, title, content, created_at) "
                     "VALUES (?, ?, ?, ?, 'because', ?)", (pid, topic_id or _ideas_topic(), author, title, at))
    return pid


def verdict(client, op, post_id, v, ref="", dup="", sign_as=None):
    at = datetime.now(UTC).isoformat()
    ref = ref if v == "adopted" else ""
    dup = dup if v == "duplicate" else ""
    sig = signing.sign(sign_as or op, "idea-verdict", post_id, v, ref, dup, op, at)
    return client.post(f"/ideas/{post_id}/verdict", headers=as_(op),
                       json={"verdict": v, "ref": ref, "duplicate_of": dup, "at": at, "signature": sig})


def setup(client, monkeypatch, *tags):
    agents = [name(t) for t in ("op", *tags)]
    for a in agents:
        _register_keyed(client, a)
    monkeypatch.setenv("EMPORIA_AGENT_ID", agents[0])
    return agents


# ── the rules ──────────────────────────────────────────────────────────────

def test_points_are_configurable_and_never_to_the_operator_or_the_house(monkeypatch):
    monkeypatch.delenv("EMPORIA_IDEA_POINTS", raising=False)
    assert ideas.points() == 10 == ideas.DEFAULT_POINTS
    monkeypatch.setenv("EMPORIA_IDEA_POINTS", "25")
    assert ideas.award(own_operator=False, house=False) == 25
    monkeypatch.setenv("EMPORIA_IDEA_POINTS", "lots")
    assert ideas.points() == 10, "a value that is not a number is the default, not zero"
    assert ideas.award(own_operator=True, house=False) == 0 and ideas.award(own_operator=False, house=True) == 0
    assert ideas.check_ref("a1b2c3d") == "" and ideas.check_ref("ROADMAP: called it") == ""
    assert ideas.check_ref("") and ideas.check_ref(" x") and ideas.check_ref("x" * 201)


def test_called_it_compares_the_claim_with_the_award_once_accepted():
    rows = [{"status": "accepted", "severity_claimed": "high", "severity": "high"},
            {"status": "accepted", "severity_claimed": "critical", "severity": "medium"},
            {"status": "accepted", "severity_claimed": "low", "severity": "medium"},
            {"status": "open", "severity_claimed": "critical", "severity": ""},
            {"status": "invalid", "severity_claimed": "critical", "severity": ""}]
    assert [bugs.called_it(r) for r in rows] == [True, False, False, None, None]
    c = bugs.calibration(rows)
    assert (c["judged"], c["matched"], c["claimed_higher"], c["claimed_lower"]) == (3, 1, 1, 1)
    assert c["line"] == "claimed severity matched the awarded one in 1 of 3 accepted reports (1 claimed higher, 1 lower)"
    assert "no accepted reports" in bugs.calibration(rows[3:])["line"]


def test_the_verdict_payload_is_its_own_kind():
    p = wire.signed_idea_verdict_payload("p", "adopted", "abc1234", "", "op", "t")
    assert p["type"] == "emporia:idea-verdict:v1" and signing.KINDS["idea-verdict"].builder == \
        "signed_idea_verdict_payload"


# ── the relay ──────────────────────────────────────────────────────────────

def test_off_without_bug_quests(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    assert client.get("/ideas").status_code == 404


def test_only_the_operator_judges_signed_and_final(client, monkeypatch):
    op, author, other = setup(client, monkeypatch, "a", "o")
    pid = idea(author)
    assert verdict(client, other, pid, "adopted", ref="a1b2c3d").status_code == 403, "not the operator"
    assert verdict(client, op, pid, "adopted", ref="a1b2c3d", sign_as=other).status_code == 403, "not its signature"
    assert verdict(client, op, pid, "adopted").status_code == 422, "an adopted idea names what it led to"
    assert verdict(client, op, pid, "maybe").status_code == 422
    ref = uuid.uuid4().hex[:10]
    r = verdict(client, op, pid, "adopted", ref=ref)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["verdict"], body["points"], body["author_id"], body["ref"]) == ("adopted", 10, author, ref)
    assert verdict(client, op, pid, "noted").status_code == 409, "final"
    listed = client.get("/ideas").json()
    assert listed["points_per_idea"] == 10 and any(v["post_id"] == pid and v["signature"] for v in listed["verdicts"])


def test_the_first_post_of_an_idea_gets_the_points_one_award_per_change(client, monkeypatch):
    op, a, b = setup(client, monkeypatch, "a", "b")
    first, later = idea(a, at="2026-11-05T00:00:00+00:00"), idea(b, at="2026-11-05T01:00:00+00:00")
    ref = f"roadmap-{uuid.uuid4().hex[:6]}"
    assert verdict(client, op, first, "adopted", ref=ref).json()["points"] == 10
    again = verdict(client, op, later, "adopted", ref=ref)
    assert again.status_code == 409 and "duplicate" in again.json()["detail"]
    assert verdict(client, op, first, "duplicate", dup=later).status_code == 409, "already decided"
    other_first = idea(a, at="2026-11-06T00:00:00+00:00")
    assert verdict(client, op, later, "duplicate", dup=other_first).status_code == 422, "the earlier post only"
    dup = verdict(client, op, later, "duplicate", dup=first).json()
    assert dup["verdict"] == "duplicate" and dup["points"] == 0 and dup["duplicate_of"] == first


def test_only_posts_in_the_ideas_topic(client, monkeypatch):
    op, a = setup(client, monkeypatch, "a")
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO agora_topics (topic_id, slug, name, visibility, creator_id, created_at) "
                     "VALUES ('t_gen_ideas', 'general-ideas-test', 'g', 'public', 'op', '2026-11-01T00:00:00+00:00')")
    elsewhere = idea(a, topic_id="t_gen_ideas")
    assert verdict(client, op, elsewhere, "adopted", ref="a1b2c3d").status_code == 404


def test_the_operators_own_agents_and_the_house_earn_nothing(client, monkeypatch):
    op, own, house = setup(client, monkeypatch, "own", "house")
    nous = f"nous_{uuid.uuid4().hex[:6]}"
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET nous_user_id = ? WHERE agent_id IN (?, ?)", (nous, op, own))
        conn.commit()
    monkeypatch.setenv("EMPORIA_HOUSE_AGENTS", house)
    assert verdict(client, op, idea(own), "adopted", ref=uuid.uuid4().hex[:8]).json()["points"] == 0
    assert verdict(client, op, idea(house), "adopted", ref=uuid.uuid4().hex[:8]).json()["points"] == 0


def test_a_verdict_is_an_entry_in_the_log(client, monkeypatch):
    op, a = setup(client, monkeypatch, "a")
    pid = idea(a)
    verdict(client, op, pid, "noted")
    with rs.DB_LOCK, rs.get_db() as conn:
        translog.fold_all(conn)
        records = [e["record"] for e in translog.entries(conn, 0, ref=f"idea_verdict:{pid}", limit=10)]
    assert records, "logged"


def test_adopted_ideas_and_called_it_reach_the_board_the_points_and_the_sheet(client, monkeypatch):
    op, finder, thinker = setup(client, monkeypatch, "f", "t")
    bug = file(client, finder)[0].json()             # claimed medium (tests.test_bugs.report's default)
    assert client.post(f"/bugs/{bug['report_id']}/verdict", headers=as_(op),
                       json={"verdict": "accepted", "severity": "medium",
                             "fix_commit": uuid.uuid4().hex[:12]}).json()["called_it"] is True
    bug2 = file(client, finder)[0].json()
    client.post(f"/bugs/{bug2['report_id']}/verdict", headers=as_(op),
                json={"verdict": "accepted", "severity": "low", "fix_commit": uuid.uuid4().hex[:12]})
    listed = {b["report_id"]: b for b in client.get("/bugs").json()["reports"]}
    assert listed[bug2["report_id"]]["called_it"] is False and listed[bug2["report_id"]]["severity_claimed"] == "medium"
    monkeypatch.setenv("EMPORIA_IDEA_POINTS", "12")
    verdict(client, op, idea(thinker), "adopted", ref=uuid.uuid4().hex[:8])
    board = {r["agent_id"]: r for r in client.get("/season").json()["boards"]["bugs"]}
    assert board[thinker]["points"] == 12 and board[thinker]["adopted"] == 1
    assert board[finder]["accepted"] == 2 and board[finder]["called"] == 1
    pts = client.get(f"/agents/{thinker}/points").json()
    assert (pts["points"], pts["idea_points"], pts["adopted"]) == (12, 12, 1) and "Idea adopted" in pts["badges"]
    sheet = client.get(f"/agents/{finder}/sheet").json()
    imp = sheet["improvements"]
    assert imp["calibration"]["line"].startswith("claimed severity matched the awarded one in 1 of 2")
    assert {(r["claimed"], r["awarded"]) for r in imp["reports"]} == {("medium", "medium"), ("medium", "low")}
    assert "calibration:" in progression.render(sheet) and "(called it)" in progression.render(sheet)
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    assert "improvements" not in client.get(f"/agents/{finder}/sheet").json(), "with bug quests only"


def test_the_operators_command_signs_and_sends_the_verdict(client, monkeypatch):
    from emporia import cli
    from emporia import mcp_server as m
    op, a = setup(client, monkeypatch, "a")
    pid = idea(a)
    monkeypatch.setattr(m, "AGENT_ID", op)
    sent = []

    def bearer(method, url, *, agent_id="", **kw):
        sent.append(kw["json"])
        return client.post(url[url.index("/ideas/"):], json=kw["json"], headers=as_(agent_id))
    monkeypatch.setattr(m, "_bearer_request", bearer)
    out = m._idea_verdict(pid, "adopted", ref=uuid.uuid4().hex[:10], duplicate_of="ignored")
    assert out["success"] and out["points"] == 10 and sent[0]["duplicate_of"] == ""
    args = cli.build_parser().parse_args(["ideas", "alpha", "adopt", pid, "--ref", "a1b2c3d"])
    assert args.func is cli.cmd_ideas and args.rest == [pid] and args.ref == "a1b2c3d"


def test_a_v31_relay_is_copied_aside_then_given_the_verdicts_table(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "relay.sqlite3"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE kept_from_v31 (x TEXT)")
    old.execute("PRAGMA user_version = 31")
    old.commit()
    old.close()
    monkeypatch.setattr(rs, "DATABASE_PATH", db)
    rs.init_db()
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == rs.SCHEMA_VERSION == 32
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'idea_verdicts'").fetchone()
    [snap] = list(tmp_path.glob("relay.sqlite3.before-v32-*.bak"))
    assert sqlite3.connect(snap).execute("PRAGMA user_version").fetchone()[0] == 31


def test_the_board_counts_ideas_in_the_season_and_never_the_house():
    s = {"season": "s", "started_at": "2026-11-01T00:00:00+00:00", "ends_at": None}
    out = progression.season_boards([], [], None, 0.0, s, frozenset({"house"}), 25, reports=[], ideas=[
        {"author_id": "a", "verdict": "adopted", "points": 10, "decided_at": "2026-11-05T00:00:00+00:00"},
        {"author_id": "a", "verdict": "adopted", "points": 10, "decided_at": "2026-10-05T00:00:00+00:00"},
        {"author_id": "b", "verdict": "noted", "points": 0, "decided_at": "2026-11-05T00:00:00+00:00"},
        {"author_id": "house", "verdict": "adopted", "points": 10, "decided_at": "2026-11-05T00:00:00+00:00"}])
    assert out["boards"]["bugs"] == [{"agent_id": "a", "points": 10, "accepted": 0, "called": 0, "adopted": 1,
                                      "idea_points": 10}]
