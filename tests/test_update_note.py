"""The update note (2.1): one fixed, once-a-week word to the person of an agent on the previous release, and the
operator's view of how many there are. Never a change to standing, limits or visibility."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import identity, update_note, wire
from emporia import relay_server as rs
from emporia.relay_server import app

ORIGIN = "http://testserver"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51377))


def _register(client, agent, *, old_card=True, version="0.3.0", name="hermes-plugin"):
    _, pub = identity.generate_or_load_keypair(agent)
    ch = client.post("/agents/challenge").json()
    if old_card:      # the card 2.0.1 signs, written out by hand
        card = {"type": "emporia:sign-in:v2", "nonce": ch["nonce"], "relay": ORIGIN, "agent_id": agent,
                "public_key": pub.hex().lower(), "operator": "", "client": {"name": name, "version": version},
                "about": ""}
    else:
        card = wire.signed_registration_payload(ch["nonce"], ORIGIN, agent, pub.hex(), "", name, version, "")
    r = client.post("/agents/register", json={
        "agent_id": agent, "public_key_hex": pub.hex(), "challenge_id": ch["challenge_id"],
        "challenge_signature": identity.sign(card, agent), "relay": ORIGIN,
        "client_name": name, "client_version": version})
    assert r.status_code == 200, r.text
    return r


def _notes(agent):
    with rs.DB_LOCK, rs.get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM agent_inbox WHERE agent_id = ? AND event_type = ?", (agent, update_note.FRAME_TYPE))]


def _name():
    return f"nudge_{uuid.uuid4().hex[:8]}"


def test_a_v2_sign_in_leaves_one_note_and_a_second_within_a_week_none(client):
    agent = _name()
    _register(client, agent)
    notes = _notes(agent)
    assert len(notes) == 1
    assert json.loads(notes[0]["payload_json"]) == {"type": "relay_update_note", "message": update_note.TEXT}
    # signing in again, with the old card or with the old version on a new card, leaves nothing more
    _register(client, _name())     # another agent: its own note
    with rs.DB_LOCK, rs.get_db() as conn:
        assert update_note.maybe_leave(conn, agent, "hermes-plugin", "0.3.0", old_card=True) is False
    assert len(_notes(agent)) == 1


def test_after_seven_days_it_may_be_left_again(client):
    agent = _name()
    _register(client, agent)
    later = datetime.now(UTC) + timedelta(days=7, minutes=1)
    with rs.DB_LOCK, rs.get_db() as conn:
        assert update_note.maybe_leave(conn, agent, "hermes-plugin", "0.3.0", old_card=True, now=later) is True
    assert len(_notes(agent)) == 2


def test_an_older_version_on_a_current_card_gets_the_note_a_current_client_does_not(client):
    old, new = _name(), _name()
    _register(client, old, old_card=False, version="2.0.1")
    _register(client, new, old_card=False, version="2.1.0")
    assert len(_notes(old)) == 1
    assert _notes(new) == []


def test_other_clients_and_unversioned_ones_get_nothing(client):
    for name, version in (("custom", "0.1"), ("hermes-plugin", ""), ("hermes-plugin", "nightly")):
        agent = _name()
        _register(client, agent, old_card=False, version=version, name=name)
        assert _notes(agent) == [], (name, version)


def test_the_setting_off_leaves_no_note(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_UPDATE_NOTE", "off")
    agent = _name()
    _register(client, agent)
    assert _notes(agent) == []


def test_the_text_is_fixed_has_no_address_and_fits_what_a_201_plugin_shows():
    assert not re.search(r"https?:|www\.|://|\.(com|org|io|net|dev)\b", update_note.TEXT)
    assert update_note.TEXT.startswith("For your person:")
    assert "hermes plugins install emporia --force" in update_note.TEXT
    assert len(update_note.TEXT) <= 200          # a 2.0.1 plugin shows an unknown frame's message cut at 200


def test_a_201_plugin_shows_the_note_from_the_relay_in_one_line():
    """The 2.0.1 formatter (unknown frame: type, then the message cut at 200) reads it; the note itself is the
    relay's own report channel."""
    from emporia.plugins.platforms.emporia.frames import format_frame
    n = format_frame({"type": update_note.FRAME_TYPE, "message": update_note.TEXT}, "agent")
    assert n is not None and n.sender == "emporia:relay"
    assert "For your person" in n.text and "\n" not in n.text and "--force" in n.text


def test_it_changes_nothing_for_the_agent_but_its_inbox(client):
    agent = _name()
    _register(client, agent)
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT is_active, client_name, client_version FROM authorized_agents WHERE agent_id = ?",
                           (agent,)).fetchone()
    assert (row["is_active"], row["client_name"], row["client_version"]) == (1, "hermes-plugin", "0.3.0")


# ── the operator's view ─────────────────────────────────────────────────────────────────────

def test_summary_counts_by_version_and_agents_on_the_previous_release(client):
    before = client.get("/relay/summary").json()
    _register(client, _name(), version="0.3.0")
    _register(client, _name(), old_card=False, version="2.1.0")
    after = client.get("/relay/summary").json()
    assert after["clients"].get("hermes-plugin", 0) >= before["clients"].get("hermes-plugin", 0) + 2   # unchanged key
    versions = after["client_versions"]
    assert versions.get("0.3.0", 0) == before["client_versions"].get("0.3.0", 0) + 1
    assert versions.get("2.1.0", 0) == before["client_versions"].get("2.1.0", 0) + 1
    assert after["agents_on_previous_release"] == before["agents_on_previous_release"] + 1


def test_previous_count_and_versions_on_a_plain_connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE authorized_agents (agent_id TEXT, is_active INTEGER, client_name TEXT, "
                 "client_version TEXT)")
    conn.executemany("INSERT INTO authorized_agents VALUES (?, ?, ?, ?)", [
        ("a", 1, "hermes-plugin", "0.3.0"), ("b", 1, "hermes-plugin", "2.1.0"), ("c", 1, "custom", "0.1"),
        ("d", 0, "hermes-plugin", "0.3.0"), ("e", 1, "", "")])
    assert update_note.previous_count(conn) == 1
    assert update_note.versions_count(conn) == {"0.3.0": 1, "2.1.0": 1, "0.1": 1, "unknown": 1}


def test_relay_status_prints_the_line_only_when_there_are_some(monkeypatch, capsys):
    from emporia import cli

    class R:
        status_code = 200

        def __init__(self, n):
            self.n = n

        def json(self):
            return {"agents_on_previous_release": self.n}
    import httpx
    monkeypatch.setattr(cli, "relay_url", lambda: "http://x")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R(3))
    assert cli.previous_release_line() == "3 agents on the previous release"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R(1))
    assert cli.previous_release_line() == "1 agent on the previous release"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R(0))
    assert cli.previous_release_line() == ""

    def down(*a, **k):
        raise httpx.ConnectError("no")
    monkeypatch.setattr(httpx, "get", down)
    assert cli.previous_release_line() == ""


def test_the_setting_is_listed_and_documented():
    from pathlib import Path

    from emporia import relay_config
    s = relay_config.BY_NAME["EMPORIA_UPDATE_NOTE"]
    assert s.kind == "bool" and s.default == "1"
    assert "`EMPORIA_UPDATE_NOTE`" in (Path(__file__).resolve().parents[1] / "README.md").read_text()
