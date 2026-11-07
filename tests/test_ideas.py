"""Launch additions: adopted ideas, "called it", and the improvement board.

The operator's signed verdict on a post in the ideas topic, reusing the bug verdict's
mechanism (operator only, final, logged); a reporter's claimed severity beside the
awarded one; adopted ideas' points on the season's bug board and the sheet. Since the
security review's pass 2 (P1) a verdict is signed by the operator's verdict key, never the
operator agent's own."""
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from emporia import bugs, ideas, progression, signing, translog, wire
from emporia import relay_server as rs
from tests import verdict_key as vk
from tests.test_bugs import file
from tests.test_emporia import _register_keyed


from tests.ids import stable_of  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,bugs")
    monkeypatch.delenv("EMPORIA_IDEA_POINTS", raising=False)
    monkeypatch.delenv("EMPORIA_HOUSE_AGENTS", raising=False)
    vk.register(monkeypatch)
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
    from tests.topic_posts import seed_post_row
    _ideas_topic()
    slug = "ideas"
    if topic_id:
        with rs.DB_LOCK, rs.get_db() as conn:
            slug = conn.execute("SELECT slug FROM agora_topics WHERE topic_id = ?", (topic_id,)).fetchone()[0]
    return seed_post_row(author, slug, f"{title}\nbecause", at, kind="idea")


def verdict(client, op, post_id, v, ref="", dup="", key=vk.KEY):
    return client.post(f"/ideas/{post_id}/verdict", headers=as_(op), json=vk.idea(post_id, v, ref, dup, key=key))


def setup(client, monkeypatch, *tags):
    agents = [name(t) for t in ("op", *tags)]
    for a in agents:
        _register_keyed(client, a)
    monkeypatch.setenv("EMPORIA_OPERATOR_STABLE_ID", stable_of(agents[0]))
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


def test_the_verdict_payload_is_its_own_kind_and_no_agents():
    p = wire.signed_idea_verdict_payload("p", "adopted", "abc1234", "", "relay", "k", "t")
    assert p["type"] == "emporia:idea-verdict:v2" and "idea-verdict" not in signing.KINDS
    assert (p["relay"], p["verdict_key"]) == ("relay", "k")


# ── the relay ──────────────────────────────────────────────────────────────

def test_off_without_bug_quests(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    assert client.get("/ideas").status_code == 404


def test_only_the_operator_judges_signed_and_final(client, monkeypatch):
    op, author, other = setup(client, monkeypatch, "a", "o")
    pid = idea(author)
    assert verdict(client, other, pid, "adopted", ref="a1b2c3d").status_code == 403, "not the operator"
    stranger = vk.ed25519.Ed25519PrivateKey.generate()
    assert verdict(client, op, pid, "adopted", ref="a1b2c3d", key=stranger).status_code == 403, "not its key"
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
        conn.execute("UPDATE authorized_agents SET operator_label = ? WHERE agent_id IN (?, ?)", (nous, op, own))
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
                       json=vk.bug(bug["report_id"], "accepted", "medium",
                                   uuid.uuid4().hex[:12])).json()["called_it"] is True
    bug2 = file(client, finder)[0].json()
    client.post(f"/bugs/{bug2['report_id']}/verdict", headers=as_(op),
                json=vk.bug(bug2["report_id"], "accepted", "low", uuid.uuid4().hex[:12]))
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


def test_the_operators_command_signs_with_the_verdict_key_and_sends_the_verdict(client, monkeypatch, capsys,
                                                                                 tmp_path):
    """`emporia ideas NAME adopt POST --ref X`: signed with the operator's verdict key — a
    person's key in the signer's folder, made by `emporia operator NAME verdict-key new` —
    and sent with the operator's sign-in. A person's command, never an MCP tool."""
    import json
    from pathlib import Path

    from emporia import cli, operator_verdicts
    op, a = setup(client, monkeypatch, "a")
    pid = idea(a)
    profile = tmp_path / "alpha"
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "signer"))
    monkeypatch.setattr(cli, "_agent_profiles", lambda root: [(profile, {})])
    monkeypatch.setattr(cli, "_agent", lambda n: (profile, op, {"EMPORIA_AGENT_ID": op}))
    monkeypatch.setattr(cli, "_key_file", lambda p, aid, which: Path(tmp_path / "keys" / "k"))
    sent = []

    def signed(agent_id, key_dir, path, method="GET", body=None):
        sent.append((path, body))
        r = client.request(method, "/" + path, json=body, headers=as_(agent_id))
        return r.status_code, r.json()
    monkeypatch.setattr(cli, "_signed_get", signed)
    ref = uuid.uuid4().hex[:10]
    args = cli.build_parser().parse_args(["ideas", "alpha", "adopt", pid, "--ref", ref])
    assert args.func is cli.cmd_ideas and args.rest == [pid] and args.ref == ref
    monkeypatch.setattr(cli, "_TTY", False)
    assert cli.cmd_ideas(args) == 1 and "only a person" in capsys.readouterr().out and not sent
    monkeypatch.setattr(cli, "_TTY", True)
    for k in [k for k in os.environ if k.startswith("HERMES_SESSION_")]:
        monkeypatch.delenv(k)
    assert cli.cmd_ideas(args) == 1 and "no verdict key here" in capsys.readouterr().out
    keygen = cli.build_parser().parse_args(["operator", "alpha", "verdict-key", "new"])
    assert cli.cmd_operator(keygen) == 0
    shown = capsys.readouterr().out
    pub = operator_verdicts.public_hex()
    assert pub in shown and f"{operator_verdicts.ENV}={pub}" in shown, shown
    assert oct(operator_verdicts.key_path().stat().st_mode & 0o777) == "0o600"
    assert cli.cmd_ideas(args) == 1 and "the relay takes verdicts from the verdict key" in capsys.readouterr().out
    monkeypatch.setenv(operator_verdicts.ENV, pub)            # the person registers it on the relay
    sent.clear()
    assert cli.cmd_ideas(args) == 0, capsys.readouterr().out
    out = json.loads(capsys.readouterr().out)
    assert out["points"] == 10 and out["verdict_key"] == pub
    assert sent[-1][0] == f"ideas/{pid}/verdict" and sent[-1][1]["duplicate_of"] == ""
    src = (Path(__file__).parent.parent / "src" / "emporia" / "mcp_server.py").read_text()
    assert "/verdict" not in src and "operator_verdicts" not in src, "no MCP tool reaches a verdict"






def test_the_board_counts_ideas_in_the_season_and_never_the_house():
    s = {"season": "s", "started_at": "2026-11-01T00:00:00+00:00", "ends_at": None}
    out = progression.season_boards([], [], None, 0.0, s, frozenset({"house"}), 25, reports=[], ideas=[
        {"author_id": "a", "verdict": "adopted", "points": 10, "decided_at": "2026-11-05T00:00:00+00:00"},
        {"author_id": "a", "verdict": "adopted", "points": 10, "decided_at": "2026-10-05T00:00:00+00:00"},
        {"author_id": "b", "verdict": "noted", "points": 0, "decided_at": "2026-11-05T00:00:00+00:00"},
        {"author_id": "house", "verdict": "adopted", "points": 10, "decided_at": "2026-11-05T00:00:00+00:00"}])
    assert out["boards"]["bugs"] == [{"agent_id": "a", "points": 10, "accepted": 0, "called": 0, "adopted": 1,
                                      "idea_points": 10}]
