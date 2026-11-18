"""The setup a game was played with (`emporia.setup_snapshot`): declared by the agent, kept when it changes, shown on the
sheet, the session's model record and the game export. Never proven, never checked, never a reason to refuse a game.
A client from before 2.1 sends none, and the record says unknown."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import relay_server as rs
from emporia import schema_sql, setup_snapshot
from emporia.routes import games_export, sessions
from emporia.module_sdk import SessionState

DECLARED = {"model": "vendor/model-1", "template": "player", "wake": "direct", "rounds": "4h", "auto_reveal": "on"}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema_sql.SCHEMA_SQL)
    return conn


# ── pure ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_clean_keeps_only_known_plain_parts_and_never_raises():
    got = setup_snapshot.clean({**DECLARED, "extra": "x", "wake": "bad value!", "auto_reveal": "maybe", "rounds": 4})
    assert got == {"model": "vendor/model-1", "template": "player"}
    assert setup_snapshot.clean(None) == setup_snapshot.clean("x") == setup_snapshot.clean([]) == {}
    assert setup_snapshot.clean({"model": "a" * 200}) == {}


def test_compose_adds_the_registered_client_only_to_a_declared_setup():
    snap = setup_snapshot.compose(DECLARED, "hermes-plugin", "2.1.0")
    assert snap["client"] == {"name": "hermes-plugin", "version": "2.1.0"} and snap["model"] == "vendor/model-1"
    assert setup_snapshot.compose({}, "hermes-plugin", "2.0.1") == {}            # a 2.0.1 client declares none: unknown
    assert setup_snapshot.view(None) is None and setup_snapshot.view(snap)["declared"] is True


def test_build_reads_the_setting_names_it_knows_and_leaves_the_rest_out(monkeypatch, tmp_path):
    monkeypatch.setenv("EMPORIA_MODEL", "vendor/model-2")
    monkeypatch.setenv("EMPORIA_CLIENT", "sdk")
    got = setup_snapshot.build({"EMPORIA_WAKE": "open", "EMPORIA_ROUNDS_EVERY": "8h",
                                setup_snapshot.AUTO_REVEAL_ENV: "off"}, home=tmp_path)
    assert got == {"model": "vendor/model-2", "wake": "open", "rounds": "8h", "auto_reveal": "off"}
    assert "template" not in setup_snapshot.build({}, home=tmp_path)             # no template was ever chosen
    monkeypatch.delenv("EMPORIA_MODEL")
    assert "model" not in setup_snapshot.build({}, home=tmp_path)                # unknown, not guessed


def test_build_declares_a_template_and_its_cadence_when_the_profile_chose_one(tmp_path):
    from emporia import templates
    templates.write_record(tmp_path, {"template": "player"})
    got = setup_snapshot.build({}, home=tmp_path)
    assert got["template"] == "player" and got["rounds"] == templates.get("player").cadence


# ── the table: kept when it changed ──────────────────────────────────────────────────────────────────────

def test_a_setup_is_kept_only_when_it_changes_and_the_story_names_the_dates():
    conn = _db()
    assert setup_snapshot.story(conn, "s1") == {"known": False, "note": setup_snapshot.NOTE}   # never declared
    a = setup_snapshot.compose(DECLARED, "sdk", "2.1.0")
    assert setup_snapshot.record(conn, "s1", a, "2026-11-10T10:00:00+00:00") is True
    assert setup_snapshot.record(conn, "s1", a, "2026-11-11T10:00:00+00:00") is False         # same: no new row
    assert "changed_on" not in setup_snapshot.story(conn, "s1")
    b = {**a, "model": "vendor/model-9"}
    assert setup_snapshot.record(conn, "s1", b, "2026-11-12T09:00:00+00:00") is True
    story = setup_snapshot.story(conn, "s1")
    assert story["current"]["model"] == "vendor/model-9"
    assert story["changed_on"] == ["2026-11-12"] and "setup changed on 2026-11-12" in story["changed_note"]
    assert story["setups_seen"] == 2 and story["note"] == setup_snapshot.NOTE and story["current"]["declared"] is True
    assert setup_snapshot.record(conn, "s1", {}, "2026-11-13T00:00:00+00:00") is False        # nothing declared: nothing kept
    assert setup_snapshot.record(conn, "s1", a, "2026-11-14T00:00:00+00:00") is True          # back to the first: a change
    assert setup_snapshot.story(conn, "s1")["changed_on"] == ["2026-11-12", "2026-11-14"]


def test_the_setup_of_a_game_is_the_one_in_force_when_it_joined():
    conn = _db()
    a = setup_snapshot.compose(DECLARED, "sdk", "2.1.0")
    b = {**a, "wake": "closed"}
    setup_snapshot.record(conn, "s1", a, "2026-11-10T10:00:00+00:00")
    setup_snapshot.record(conn, "s1", b, "2026-11-12T10:00:00+00:00")
    assert setup_snapshot.at_time(conn, "s1", "2026-11-11T00:00:00+00:00")["wake"] == "direct"
    assert setup_snapshot.at_time(conn, "s1", "2026-11-12T10:00:02+00:00")["wake"] == "closed"   # the join that declared it
    assert setup_snapshot.at_time(conn, "s1", "2026-11-13T00:00:00+00:00")["wake"] == "closed"
    assert setup_snapshot.at_time(conn, "s1", "2026-11-09T00:00:00+00:00") is None               # before any declaration: unknown
    assert setup_snapshot.at_time(conn, "nobody", "2026-11-13T00:00:00+00:00") is None


# ── on a relay ───────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    monkeypatch.setattr(rs.AGENT_REGISTRY, "check_rate_limit", lambda agent_id, *a: True)
    return TestClient(rs.app, client=("127.0.0.1", 51961))


def _register(client, tag, client_name="hermes-plugin", version="2.1.0"):
    from emporia.identity import get_public_key_hex
    name = f"su{tag}"
    r = client.post("/agents/register", json={"agent_id": name, "public_key_hex": get_public_key_hex(name),
                                              "client_name": client_name, "client_version": version})
    assert r.status_code == 200, r.text
    rs.forget_who()
    return name


def test_the_sheet_shows_the_current_setup_and_the_date_it_changed(client):
    tag = uuid.uuid4().hex[:6]
    ann, old = _register(client, f"{tag}a"), _register(client, f"{tag}o", "hermes-plugin", "2.0.1")
    sessions._note_setup(ann, DECLARED)
    sessions._note_setup(ann, DECLARED)                                     # the second join: nothing new
    sessions._note_setup(old, None)                                          # a 2.0.1 client sends none
    sessions._note_setup(old, {})
    sheet = client.get(f"/agents/{ann}/sheet").json()
    assert sheet["setup"]["known"] and sheet["setup"]["current"]["model"] == "vendor/model-1"
    assert sheet["setup"]["current"]["client"] == {"name": "hermes-plugin", "version": "2.1.0"}
    assert "changed_on" not in sheet["setup"] and "not proven" in sheet["setup"]["note"]
    assert client.get(f"/agents/{old}/sheet").json()["setup"] == {"known": False, "note": setup_snapshot.NOTE}
    sessions._note_setup(ann, {**DECLARED, "model": "vendor/model-3"})
    sheet = client.get(f"/agents/{ann}/sheet").json()
    assert sheet["setup"]["current"]["model"] == "vendor/model-3" and len(sheet["setup"]["changed_on"]) == 1
    from emporia import progression
    text = progression.render(sheet)
    assert "Setup" in text and "vendor/model-3" in text and "declared, not proven" in text and "setup changed on" in text


def test_the_requests_take_an_optional_setup_and_a_2_0_1_body_is_still_valid():
    assert rs.JoinSessionRequest(agent_id="a").setup is None and rs.CreateSessionRequest(module_type="m").setup is None
    assert rs.JoinSessionRequest(agent_id="a", setup=DECLARED).setup == DECLARED
    assert rs.JoinSessionRequest(agent_id="a", unknown_future_field=1).agent_id == "a"            # extras are ignored


def test_a_session_record_and_the_export_carry_each_seats_setup_or_unknown(client):
    tag = uuid.uuid4().hex[:6]
    ann, old = _register(client, f"{tag}a"), _register(client, f"{tag}o", "hermes-plugin", "2.0.1")
    sessions._note_setup(ann, DECLARED)
    sid = f"sess_su{tag}"
    now = datetime.now(UTC)
    rs.save_session(sid, "emporia:blotto:v2", {}, {}, SessionState(data={}, current_agent=ann, step_number=0),
                    ann, 0, "completed", [ann, old], (now - timedelta(minutes=5)).isoformat(), now.isoformat())
    with rs.DB_LOCK, rs.get_db() as conn:
        for who in (ann, old):
            conn.execute("INSERT OR REPLACE INTO participants (session_id, agent_id, joined_at) VALUES (?,?,?)",
                         (sid, who, (now + timedelta(minutes=1)).isoformat()))
        conn.commit()
    # joined after it declared, so the declaration is the one in force
    got = client.get(f"/sessions/{sid}/models").json()
    assert got["players"][ann]["setup"]["model"] == "vendor/model-1" and got["players"][ann]["setup"]["declared"] is True
    assert got["players"][old]["setup"] is None and "setup_note" in got
    s = rs.load_session(sid)
    seats = games_export._seats(s)
    out = games_export._setups(s, seats)
    assert out["declared"] is True and out["seats"]["0"]["model"] == "vendor/model-1" and out["seats"]["1"] is None
