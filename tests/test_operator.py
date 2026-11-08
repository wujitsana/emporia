"""The operator section: who is the relay's operator (a signed check, never a claim), the
operator's desk (queues, expedition, house agents, health, season), the commands it
suggests, and the key guard over them."""
import base64
import importlib.util
import json
import os
import shlex
import stat
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from emporia import operator_desk, playbook
from emporia import relay_server as rs
from emporia.routes import operator as op_routes
from conftest import header_shim_off
from tests.test_emporia import _register, _register_keyed

REPO = Path(__file__).resolve().parent.parent
VECTORS = REPO / "dashboard" / "src" / "operatorCommands.vectors.json"


from tests.ids import house_env, stable_of  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


guard = _load("emporia_key_guard_op", REPO / "hermes_plugin" / "emporia" / "key_guard.py")
setup_cli = _load("emporia_setup_cli_op", REPO / "hermes_plugin" / "emporia" / "setup_cli.py")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    monkeypatch.delenv("EMPORIA_HOUSE_STABLE_IDS", raising=False)
    monkeypatch.delenv("EMPORIA_EXPEDITIONS_TOPIC", raising=False)
    return TestClient(rs.app, client=("127.0.0.1", 51301))


def as_(agent):
    return {"x-emporia-agent-id": agent}


def name(tag):
    return f"op_{tag}_{uuid.uuid4().hex[:6]}"


def setup(client, monkeypatch, *tags):
    agents = [name(t) for t in ("op", *tags)]
    for a in agents:
        _register_keyed(client, a)
    monkeypatch.setenv("EMPORIA_OPERATOR_STABLE_ID", stable_of(agents[0]))
    return agents


def _topic(slug, creator="someone"):
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO agora_topics (topic_id, slug, name, visibility, creator_id, created_at) "
                     "VALUES (?, ?, ?, 'public', ?, '2026-11-01T00:00:00+00:00')", (f"t_{slug}", slug, slug, creator))
        conn.commit()
        return conn.execute("SELECT topic_id FROM agora_topics WHERE slug = ?", (slug,)).fetchone()[0]


def _post(slug, author, title, at, deleted=0):
    from tests.topic_posts import seed_post_row
    _topic(slug)
    pid = seed_post_row(author, slug, f"{title}\nbecause it would help", at, kind="idea")
    if deleted:
        with rs.DB_LOCK, rs.get_db() as conn:
            conn.execute("UPDATE posts SET is_deleted = 1 WHERE post_id = ?", (pid,))
            conn.commit()
    return pid


# ── who is the operator: signed, never claimed ───────────────────────────────

def test_whoami_answers_for_the_signed_in_caller_only(client, monkeypatch):
    op, other = setup(client, monkeypatch, "other")
    assert client.get("/operator/whoami").status_code == 401
    me = client.get("/operator/whoami", headers=as_(op)).json()
    assert me["agent_id"] == op and me["is_operator"] is True and me["operator_configured"] is True
    them = client.get("/operator/whoami", headers=as_(other)).json()
    assert them == {**them, "agent_id": other, "is_operator": False}
    assert op not in json.dumps(them), "whoami tells a caller about itself, not who the operator is"


def test_operator_detection_cannot_be_spoofed(client, monkeypatch):
    op, other = setup(client, monkeypatch, "other")
    with header_shim_off():
        # P261: a header naming the operator is no credential.
        assert client.get("/operator/whoami", headers={"X-Emporia-Agent-Id": op}).status_code == 401
        assert client.get("/operator/status", headers={"X-Emporia-Agent-Id": op}).status_code == 401
        # Nor a query naming it, beside the caller's own token.
        token = rs._jwt_encode({"agent_id": other, "stable_id": rs.AGENT_REGISTRY.get_stable_id(other),
                                "relay_id": rs.relay_id(), "iat": int(time.time()),
                                "issued": time.time(), "exp": int(time.time() + 600)})
        r = client.get(f"/operator/whoami?agent_id={op}", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200 and r.json()["agent_id"] == other and r.json()["is_operator"] is False
        assert client.get(f"/operator/status?agent_id={op}",
                          headers={"Authorization": f"Bearer {token}"}).status_code == 403
        # A token naming the operator, not signed with the relay's secret.
        body = base64.urlsafe_b64encode(json.dumps({"agent_id": op, "relay_id": rs.relay_id(),
                                                    "exp": int(time.time() + 600)}).encode()).rstrip(b"=").decode()
        forged = f"{body}.{'0' * 64}"
        assert client.get("/operator/whoami", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
        assert client.get("/operator/status", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
        # A real token, for another relay.
        elsewhere = rs._jwt_encode({"agent_id": op, "relay_id": "another-relay", "iat": int(time.time()),
                                    "issued": time.time(), "exp": int(time.time() + 600)})
        assert client.get("/operator/status", headers={"Authorization": f"Bearer {elsewhere}"}).status_code == 401


def test_no_operator_configured_means_nobody_is_one(client, monkeypatch):
    a = name("lone")
    _register(client, a)
    monkeypatch.delenv("EMPORIA_OPERATOR_STABLE_ID", raising=False)
    me = client.get("/operator/whoami", headers=as_(a)).json()
    assert me["is_operator"] is False and me["operator_configured"] is False
    r = client.get("/operator/status", headers=as_(a))
    assert r.status_code == 403 and "no relay operator is configured" in r.json()["detail"]


def test_every_other_agent_is_refused_the_desk_and_the_snapshot(client, monkeypatch):
    op, player = setup(client, monkeypatch, "player")
    assert client.get("/operator/status").status_code == 401
    assert client.get("/operator/status", headers=as_(player)).status_code == 403
    assert client.post("/operator/snapshot", headers=as_(player)).status_code == 403
    assert client.get("/operator/status", headers=as_(op)).status_code == 200


# ── the desk ──────────────────────────────────────────────────────────────────

def test_without_the_labs_module_the_desk_still_answers(client, monkeypatch):
    op, = setup(client, monkeypatch)
    monkeypatch.setenv("EMPORIA_LABS", "0")
    d = client.get("/operator/status", headers=as_(op)).json()
    assert d["labs"]["on"] is False and "bugs" not in d and "ideas" not in d, "no idea queue: there is no verdict"
    assert d["health"]["version"]


def test_the_expedition_is_the_operators_newest_post_there(client, monkeypatch):
    op, other = setup(client, monkeypatch, "other")
    monkeypatch.setenv("EMPORIA_EXPEDITIONS_TOPIC", f"exp-{uuid.uuid4().hex[:6]}")
    topic = os.environ["EMPORIA_EXPEDITIONS_TOPIC"]
    d = client.get("/operator/status", headers=as_(op)).json()["expedition"]
    assert d == {"topic": topic, "topic_exists": False, "current": None}
    _post(topic, op, "Week 1: trade a service", "2026-11-02T00:00:00+00:00")
    week2 = _post(topic, op, "Week 2: play chess", "2026-11-04T00:00:00+00:00")
    _post(topic, other, "a reply, not an expedition", "2026-11-05T00:00:00+00:00")
    cur = client.get("/operator/status", headers=as_(op)).json()["expedition"]["current"]
    assert cur["post_id"] == week2 and cur["posted_at"].startswith("2026-11-04") and cur["age_days"] >= 0


def test_house_agents_their_activity_and_the_step_back(client, monkeypatch):
    op, house_a, player = setup(client, monkeypatch, "house", "player")
    house_env(monkeypatch, house_a)
    monkeypatch.setenv("EMPORIA_HOUSE_STABLE_IDS", os.environ["EMPORIA_HOUSE_STABLE_IDS"] + "," + "ab" * 32)
    from datetime import UTC, datetime
    _post("lobby", house_a, "hello", datetime.now(UTC).isoformat())
    _post("lobby", player, "hi", datetime.now(UTC).isoformat())
    h = client.get("/operator/status", headers=as_(op)).json()["house"]
    by = {a["agent_id"]: a for a in h["agents"]}
    assert by[house_a]["registered"] and by[house_a]["active"] and by[house_a]["last_active_at"]
    assert list(by) == [house_a] and h["ids_not_registered"] == 1, "a stable id nobody registered yet is counted, not named"
    sb = h["step_back"]
    assert sb["at"] == 4 and sb["busy_window_s"] == 3600 and sb["others_active"] >= 1
    assert sb["stepping_back"] == (sb["others_active"] >= 4)


def test_health_season_and_a_snapshot(client, monkeypatch, tmp_path):
    op, = setup(client, monkeypatch)
    monkeypatch.setattr(rs, "DATABASE_PATH", rs.DATABASE_PATH)
    before = client.get("/operator/status", headers=as_(op)).json()
    h = before["health"]
    assert h["schema"]["code"] == h["schema"]["database"] and h["db_bytes"] > 0 and h["uptime_s"] >= 0
    assert set(h["rate_limit_hits"]) == {"ip", "agent", "since"} and h["active_agents"] >= 0
    assert before["season"]["season"] and "boards" in before["season"]
    for old in op_routes.snapshots(Path(rs.DATABASE_PATH)):
        os.utime(old, (time.time() - 3600, time.time() - 3600))      # not "a minute ago"
    snap = client.post("/operator/snapshot", headers=as_(op))
    assert snap.status_code == 200, snap.text
    made = Path(rs.DATABASE_PATH).with_name(snap.json()["name"])
    try:
        assert made.is_file() and stat.S_IMODE(made.stat().st_mode) == 0o600
        assert client.post("/operator/snapshot", headers=as_(op)).status_code == 429, "one a minute at most"
        last = client.get("/operator/status", headers=as_(op)).json()["health"]["last_backup"]
        assert last["name"] == made.name and last["kind"] == "operator"
    finally:
        made.unlink(missing_ok=True)


def test_a_rate_limit_hit_is_counted(client, monkeypatch):
    before = dict(rs.RATE_LIMIT_HITS)
    monkeypatch.setattr(rs.AGENT_REGISTRY, "check_rate_limit", lambda agent_id: False)
    with pytest.raises(Exception):
        rs._enforce_agent_rate_limit("anyone")
    assert rs.RATE_LIMIT_HITS["agent"] == before["agent"] + 1


# ── the commands it suggests ─────────────────────────────────────────────────

def _built(case):
    k = case["kind"]
    if k == "expedition":
        return operator_desk.expedition_command(case["name"])
    if k == "house":
        return operator_desk.house_commands(case["name"], case.get("running"))
    if k == "lab":
        return operator_desk.lab_commands(case["name"], case["item"]["lab"])
    return operator_desk.snapshot_command(case["name"])


def test_the_commands_match_the_table_the_dashboard_is_held_to():
    cases = json.loads(VECTORS.read_text())["cases"]
    assert len(cases) >= 8
    for case in cases:
        assert _built(case) == case["expect"], case


def test_a_command_carries_only_checked_ids_and_placeholders():
    assert operator_desk.lab_commands("op", "$(id)") == []
    assert operator_desk.lab_commands("Op Name", "calc") == []
    assert not hasattr(operator_desk, "idea_commands"), "an idea is adopted by a lab report, not by a command"


def test_every_suggested_command_is_a_real_command_under_the_key_guard():
    """Each command the desk suggests parses as `emporia …` (minus the `hermes` prefix, which
    the plugin passes through) and an agent's terminal cannot run any of them silently."""
    from emporia import cli
    parser = cli.build_parser()
    for case in json.loads(VECTORS.read_text())["cases"]:
        for c in case["expect"]:
            words = shlex.split(c["command"])
            assert words[:2] == ["hermes", "emporia"] and words[2] in setup_cli.PASSTHROUGH, c
            verdict = guard.pre_tool_call("terminal", {"command": c["command"]})
            if c["gate"] == operator_desk.PERSON:
                assert verdict and verdict["action"] == "block", c
            elif c["gate"] == operator_desk.APPROVE:
                assert verdict and verdict["action"] == "approve", c
            else:
                assert verdict is None, c
            parser.parse_args(words[2:])


def test_the_desk_prints_with_its_commands(client, monkeypatch):
    op, = setup(client, monkeypatch)
    d = client.get("/operator/status", headers=as_(op)).json()
    text = "\n".join(operator_desk.render(d, "opdesk"))
    assert "you are its operator" in text
    assert "hermes emporia operator opdesk snapshot" in text and "expedition" in text


# ── the key guard ─────────────────────────────────────────────────────────────

def test_relay_controls_wait_for_a_yes():
    for cmd in ("emporia house start h", "hermes emporia house stop h", "emporia house run h --once",
                "emporia operator alpha snapshot", "hermes emporia operator alpha expedition 'W' --text 't'"):
        r = guard.pre_tool_call("terminal", {"command": cmd})
        assert r and r["action"] == "approve" and "relay operator" in r["message"], cmd
    for cmd in ("emporia operator alpha", "emporia operator alpha whoami", "emporia house status h",
                "hermes emporia operator alpha status --json"):
        assert guard.pre_tool_call("terminal", {"command": cmd}) is None, cmd


def test_no_agent_tool_reaches_the_operators_write():
    """A snapshot is the person's command: no MCP tool posts one."""
    src = (REPO / "src" / "emporia" / "mcp_server.py").read_text()
    assert "/operator/snapshot" not in src


# ── the CLI ───────────────────────────────────────────────────────────────────

def test_operator_command_prints_the_desk_or_the_refusal(monkeypatch, capsys, tmp_path):
    from emporia import cli
    monkeypatch.setattr(cli, "_agent", lambda n: (tmp_path, n, {"EMPORIA_AGENT_ID": n}))
    status = {"relay_url": "http://127.0.0.1:1", "health": {"version": "0", "schema": {"code": 1, "database": 1}},
              "expedition": {"topic": "expeditions"},
              "house": {"agents": []}, "season": {"season": "season-0", "boards": []}}
    calls = []

    from emporia import mcp_server

    def call(profile, method, path, body=None, params=None):
        # The MCP client's own path check: found by the end-to-end run, a query in the path is refused.
        assert not mcp_server._unsafe_path("http://relay.test" + path), path
        calls.append((method, path, params))
        return (200, status) if path.startswith("/operator/status") else (403, {"detail": "relay operator access required"})
    monkeypatch.setattr(cli, "_operator_call", call)
    assert cli.main(["operator", "opdesk"]) == 0
    out = capsys.readouterr().out
    assert "hermes emporia operator opdesk snapshot" in out and "ideas" not in out.lower()
    assert cli.main(["operator", "opdesk", "whoami"]) == 1
    assert "relay operator access required" in capsys.readouterr().out
    assert calls[0] == ("GET", "/operator/status", {"window_s": 3600})


def test_house_stop_stops_only_the_runner_it_started(tmp_path, monkeypatch, capsys):
    """A PID file naming a process that is not a house runner (a number reused, or edited)
    stops nothing."""
    from emporia import cli
    monkeypatch.setattr(cli, "_agent", lambda n: (tmp_path, n, {"EMPORIA_AGENT_ID": n}))
    pid_file = cli._house_files(tmp_path)[0]
    pid_file.write_text(str(os.getpid()))
    monkeypatch.setattr(cli.os, "kill", lambda *a: pytest.fail("killed a process it did not start"))
    assert cli.main(["house", "stop", "h"]) == 1
    assert "not h's house runner" in capsys.readouterr().out and not pid_file.exists()
    assert cli.main(["house", "status", "h"]) == 0
    assert "not running here" in capsys.readouterr().out


def test_the_house_runner_records_its_last_pass(tmp_path):
    from emporia import house as H

    class Tools:
        def get_inbox(self, unread_only=True):
            return {"events": []}

        def relay_activity(self, window):
            return {"active_agents": 9}

        def list_sessions(self, **kw):
            return {"sessions": []}

        def list_agents(self, limit=200):
            return {"agents": []}
    h = H.House("house", Tools(), tmp_path / "state.json")
    h.step(now=time.time())
    last = json.loads((tmp_path / "state.json").read_text())["last_pass"]
    assert last["stepped_back"] == 1 and last["step_back_at"] == H.STEP_BACK_AT and last["at"]


# ── the operator's own agent ─────────────────────────────────────────────────

def test_the_operator_preset_plays_sells_and_buys_nothing():
    pb = playbook.resolve({"EMPORIA_PLAYBOOK": "operator"})
    assert pb.preset == "operator" and pb.games == playbook.NONE and not pb.sells and not pb.buys
    assert pb.answers == "anyone" and pb.runs == "model"
    assert playbook.may_play(pb, "emporia:blotto:v1") and playbook.may_sell(pb) and playbook.may_buy(pb)
    assert "verdict" not in playbook.ABOUT["operator"]


def test_the_console_names_each_agents_profile(tmp_path):
    """The NAME in the commands the dashboard shows is the agent's Hermes profile."""
    from emporia.console.app import Console, build_app
    dirs = []
    for profile, root in (("alpha", tmp_path / "profiles" / "alpha"), ("default", tmp_path)):
        keys = root / "home" / ".hermes" / "keys"
        keys.mkdir(parents=True)
        (root / "config.yaml").write_text("{}\n")
        (keys / f"op-{profile}.priv").write_bytes(b"x" * 32)
        dirs.append(keys)
    loose = tmp_path / "loose-keys"
    loose.mkdir()
    (loose / "op-loose.priv").write_bytes(b"x" * 32)
    c = TestClient(build_app(Console([*dirs, loose], "http://127.0.0.1:9", session_token=None)))
    body = c.get("/agents").json()
    assert body["agents"] == ["op-alpha", "op-default", "op-loose"]
    assert body["profiles"] == {"op-alpha": "alpha", "op-default": "default"}, "no profile, no name"
