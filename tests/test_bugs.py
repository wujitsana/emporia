"""P289: bug quests — reports committed to, judged, revealed, and every step in the Merkle log."""
import uuid

import pytest
from fastapi.testclient import TestClient

from emporia import bugs, translog
from emporia import relay_server as rs
from emporia.relay_server import app
from tests.test_emporia import _register

COMMIT, FIX = "a1b2c3d4e5f6", "f6e5d4c3b2a1"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,bugs")
    return TestClient(app, client=("127.0.0.1", 51289))


def as_(agent):
    return {"x-emporia-agent-id": agent}


def name(tag):
    return f"bq_{tag}_{uuid.uuid4().hex[:6]}"


def report(test="def test_x():\n    assert parse('') == []", title="parse crashes on empty input", sev="medium"):
    fields = bugs.report_fields(COMMIT, title, test, "parse('') raises IndexError", sev)
    salt = bugs.new_salt()
    return fields, salt, bugs.commitment(fields, salt)


def file(client, agent, *, sealed=True, fields=None):
    f, salt, c = fields or report(title=f"bug {uuid.uuid4().hex[:6]}")
    body = {"commit": COMMIT, "commitment": c, "severity": f["severity"], "sealed": sealed}
    if not sealed:
        body |= {"content": {k: f[k] for k in ("title", "test", "description")}, "salt": salt}
    return client.post("/bugs", json=body, headers=as_(agent)), f, salt, c


def log_entries(ref):
    with rs.DB_LOCK, rs.get_db() as conn:
        translog.fold_all(conn)
        return [e["record"] for e in translog.entries(conn, 0, ref=f"bug_report:{ref}", limit=100)]


# ── the rules ──────────────────────────────────────────────────────────────

def test_a_commitment_binds_the_whole_report_and_its_salt():
    f, salt, c = report()
    assert bugs.matches(c, f, salt)
    assert not bugs.matches(c, {**f, "severity": "critical"}, salt)
    assert not bugs.matches(c, f, bugs.new_salt())
    assert not bugs.matches(c, f, "")


def test_a_report_needs_a_failing_test_and_a_real_commit():
    assert "failing test" in bugs.check_report(COMMIT, "t", "", "", "low")
    assert "commit" in bugs.check_report("main", "t", "x", "", "low")
    assert bugs.check_report(COMMIT, "t", "x", "", "low") == ""


def test_spam_is_capped_and_a_run_of_invalid_reports_pauses_reporting():
    assert bugs.may_report(bugs.OPEN_CAP, 0, 0)
    assert bugs.may_report(0, 3, 0) and bugs.may_report(0, 5, 2)
    assert bugs.may_report(0, 3, 1) == "" and bugs.may_report(2, 0, 0) == ""


def test_points_only_for_accepted_and_never_to_the_operator_itself():
    assert bugs.award("high", own_operator=False) == 40 and bugs.award("high", own_operator=True) == 0
    rows = [{"reporter_id": "a", "status": "accepted", "points": 40}, {"reporter_id": "a", "status": "invalid"},
            {"reporter_id": "a", "status": "duplicate"}, {"reporter_id": "b", "status": "accepted", "points": 5}]
    assert bugs.points_of("a", rows) == {"agent_id": "a", "points": 40, "accepted": 1, "duplicate": 1,
                                         "invalid": 1, "open": 0, "badges": ["Bug finder"]}


# ── the relay ──────────────────────────────────────────────────────────────

def test_off_unless_turned_on_and_never_without_the_log(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    assert client.get("/bugs").status_code == 404
    monkeypatch.setenv("EMPORIA_GAMIFY", "bugs")
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "off")
    r = client.get("/bugs")
    assert r.status_code == 404 and "transparency log" in r.json()["detail"]
    assert "bugs" not in client.get("/relay/card").json()["gamify"]
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    assert "bugs" in client.get("/relay/card").json()["gamify"]


def test_a_sealed_report_shows_only_its_commitment_until_revealed(client, monkeypatch):
    op, finder = name("op"), name("finder")
    for a in (op, finder):
        _register(client, a)
    monkeypatch.setenv("EMPORIA_AGENT_ID", op)
    r, f, salt, c = file(client, finder)
    assert r.status_code == 200, r.text
    bug = r.json()
    assert bug["sealed"] and bug["title"] is None and bug["content"] is None and bug["send_to"] == op
    public = client.get(f"/bugs/{bug['report_id']}").text
    assert f["title"] not in public and f["test"] not in public and c in public

    [entry] = log_entries(bug["report_id"])                     # in the Merkle log at once, content-free
    assert entry["commitment"] == c and entry["status"] == "open" and entry["content_hash"] == ""
    assert f["title"] not in str(entry)

    assert client.post(f"/bugs/{bug['report_id']}/reveal", headers=as_(finder),
                       json={"content": f, "salt": salt}).status_code == 409        # not before the verdict
    v = client.post(f"/bugs/{bug['report_id']}/verdict", headers=as_(op),
                    json={"verdict": "accepted", "severity": "high", "fix_commit": FIX}).json()
    assert v["status"] == "accepted" and v["points"] == 40
    wrong = client.post(f"/bugs/{bug['report_id']}/reveal", headers=as_(finder),
                        json={"content": {**f, "title": "something else"}, "salt": salt})
    assert wrong.status_code == 422                               # a reveal must match what was logged
    ok = client.post(f"/bugs/{bug['report_id']}/reveal", headers=as_(finder), json={"content": f, "salt": salt})
    assert ok.status_code == 200 and ok.json()["content"]["test"] == f["test"]

    history = log_entries(bug["report_id"])                     # open → accepted → revealed, each kept
    assert [e["status"] for e in history] == ["open", "accepted", "accepted"]
    assert history[-1]["content_hash"] and history[-1]["revealed_at"]
    pts = client.get(f"/agents/{finder}/points").json()
    assert pts["points"] == 40 and pts["badges"] == ["Bug finder"]


def test_an_open_report_must_match_its_commitment(client):
    finder = name("open")
    _register(client, finder)
    f, salt, c = report(title="open one")
    bad = client.post("/bugs", headers=as_(finder), json={
        "commit": COMMIT, "commitment": c, "severity": "medium", "sealed": False,
        "content": {**{k: f[k] for k in ("title", "test", "description")}, "title": "changed"}, "salt": salt})
    assert bad.status_code == 422
    r, *_ = file(client, finder, sealed=False, fields=(f, salt, c))
    assert r.status_code == 200 and r.json()["title"] == "open one"
    again, *_ = file(client, finder, sealed=False, fields=(f, salt, c))
    assert again.status_code == 409                               # the same report twice


def test_only_the_operator_judges_and_a_verdict_is_final(client, monkeypatch):
    op, finder = name("op2"), name("f2")
    for a in (op, finder):
        _register(client, a)
    monkeypatch.setenv("EMPORIA_AGENT_ID", op)
    bug = file(client, finder)[0].json()
    assert client.post(f"/bugs/{bug['report_id']}/verdict", headers=as_(finder),
                       json={"verdict": "accepted", "severity": "critical", "fix_commit": FIX}).status_code == 403
    assert client.post(f"/bugs/{bug['report_id']}/verdict", headers=as_(op),
                       json={"verdict": "invalid"}).json()["status"] == "invalid"
    assert client.post(f"/bugs/{bug['report_id']}/verdict", headers=as_(op),
                       json={"verdict": "accepted", "severity": "low", "fix_commit": FIX}).status_code == 409


def test_one_fix_one_award(client, monkeypatch):
    op, a, b = name("op3"), name("a3"), name("b3")
    for x in (op, a, b):
        _register(client, x)
    monkeypatch.setenv("EMPORIA_AGENT_ID", op)
    fix = uuid.uuid4().hex[:12]
    first, second = file(client, a)[0].json(), file(client, b)[0].json()
    assert client.post(f"/bugs/{first['report_id']}/verdict", headers=as_(op),
                       json={"verdict": "accepted", "severity": "medium", "fix_commit": fix}).status_code == 200
    again = client.post(f"/bugs/{second['report_id']}/verdict", headers=as_(op),
                        json={"verdict": "accepted", "severity": "medium", "fix_commit": fix})
    assert again.status_code == 409 and "duplicate" in again.json()["detail"]
    dup = client.post(f"/bugs/{second['report_id']}/verdict", headers=as_(op),
                      json={"verdict": "duplicate", "duplicate_of": first["report_id"]}).json()
    assert dup["status"] == "duplicate" and dup["points"] == 0


def test_an_operators_agents_share_one_cap_and_the_operator_earns_nothing(client, monkeypatch):
    op, sock1, sock2 = name("op4"), name("s1"), name("s2")
    nous = f"nous_{uuid.uuid4().hex[:6]}"
    for x in (op, sock1, sock2):
        _register(client, x)
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET nous_user_id = ? WHERE agent_id IN (?, ?, ?)",
                     (nous, op, sock1, sock2))
        conn.commit()
    monkeypatch.setenv("EMPORIA_AGENT_ID", op)
    filed = [file(client, who)[0] for who in (sock1, sock2, sock1)]
    assert all(r.status_code == 200 for r in filed)
    capped = file(client, sock2)[0]
    assert capped.status_code == 429 and "already open" in capped.json()["detail"]   # one operator, one cap
    v = client.post(f"/bugs/{filed[0].json()['report_id']}/verdict", headers=as_(op),
                    json={"verdict": "accepted", "severity": "critical", "fix_commit": uuid.uuid4().hex[:12]}).json()
    assert v["status"] == "accepted" and v["points"] == 0                         # an operator does not pay itself


def test_a_security_incident_is_on_the_record_for_good(client, monkeypatch):
    op, other = name("op5"), name("o5")
    for x in (op, other):
        _register(client, x)
    monkeypatch.setenv("EMPORIA_AGENT_ID", op)
    assert client.post("/security/incidents", headers=as_(other), json={"summary": "x"}).status_code == 403
    inc = client.post("/security/incidents", headers=as_(op),
                      json={"summary": "session tokens readable in a log file", "affected": "sign-in"}).json()
    fixed = client.post(f"/security/incidents/{inc['incident_id']}/fixed", headers=as_(op), json={}).json()
    assert fixed["fixed_at"]
    with rs.DB_LOCK, rs.get_db() as conn:
        translog.fold_all(conn)
        history = [e["record"] for e in translog.entries(conn, 0, ref=f"security_incident:{inc['incident_id']}",
                                                         limit=10)]
    assert [bool(e["fixed_at"]) for e in history] == [False, True]
    assert history[0]["summary"] == "session tokens readable in a log file"
    assert any(i["incident_id"] == inc["incident_id"] for i in client.get("/security/incidents").json()["incidents"])


def test_the_agent_keeps_its_report_and_salt_and_sends_a_sealed_one_only_encrypted(monkeypatch, tmp_path):
    from emporia import identity
    from emporia import mcp_server as m

    class R:
        status_code, content = 200, b"x"

        def __init__(self, d):
            self.d = d

        def json(self):
            return self.d

    sent, posted = [], []
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    thread = {"encrypted": True}

    def bearer(method, url, **kw):
        if url.endswith("/dm/start"):
            return R(dict(thread))
        posted.append(kw["json"])
        return R({"report_id": "bug_test1", "send_to": "the_op"} if url.endswith("/bugs") else {"revealed": True})
    monkeypatch.setattr(m, "_bearer_request", bearer)
    monkeypatch.setattr(m, "send_dm", lambda to, content, **kw: sent.append((to, content, kw)) or {"success": True})
    out = m.report_bug.fn(COMMIT, "a title", "def test(): assert False", "why", "high") if hasattr(
        m.report_bug, "fn") else m.report_bug(COMMIT, "a title", "def test(): assert False", "why", "high")
    assert out["success"] and out["sent_to_operator"]
    assert "content" not in posted[0] and "salt" not in posted[0]           # the relay gets the commitment only
    [(to, content, kw)] = sent
    assert to == "the_op" and kw == {"encrypted": True} and "def test(): assert False" in content
    kept = next(tmp_path.glob("bug_reports/*/bug_test1.json"))
    assert oct(kept.stat().st_mode)[-3:] == "600"
    thread["encrypted"] = False                     # one of them has no key: nothing is sent at all
    again = m._send_sealed_bug("bug_test1", "the_op")
    assert again["sent_to_operator"] is False and len(sent) == 1
    out = m.reveal_bug.fn("bug_test1") if hasattr(m.reveal_bug, "fn") else m.reveal_bug("bug_test1")
    assert out["success"] and posted[-1]["content"]["title"] == "a title" and posted[-1]["salt"]
