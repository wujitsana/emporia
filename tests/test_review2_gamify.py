"""Review pass 2 (2026-11-06, gamify): proof tests for what it found and fixed.

`docs/review-pass-2026-11-06-gamify.md` lists every finding; each fix here has its test."""
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import pledges as PL
from emporia import relay_server as rs
from tests.test_emporia import _register
from tests.test_pledges import _drop_made_sessions, agents, make, record_action, session, set_session  # noqa: F401


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests,pledges")
    monkeypatch.delenv("EMPORIA_HOUSE_AGENTS", raising=False)
    monkeypatch.delenv("EMPORIA_PLEDGES_TRUST", raising=False)
    return TestClient(rs.app, client=("127.0.0.1", 51306))


def _hdr(agent):
    return {"x-emporia-agent-id": agent}


# ── abandoning on the opponent's turn ───────────────────────────────────────

def test_abandoning_on_the_opponents_turn_is_your_walk_not_theirs(client):
    """Found: /abandon is open to any player at any time, and standing blamed whoever's turn
    it was. A commits in blotto (now B's turn), abandons, and B took the walk-away loss in
    Craft and −2 Trust. Now the relay records who abandoned, and that player walked."""
    a, b = f"r2_a_{uuid.uuid4().hex[:6]}", f"r2_b_{uuid.uuid4().hex[:6]}"
    for x in (a, b):
        _register(client, x)
    r = client.post("/sessions", json={"module_type": "emporia:blotto:v2", "creator_agent_id": a, "config": {}},
                    headers=_hdr(a))
    sid = r.json()["session_id"]
    assert client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers=_hdr(b)).status_code == 200
    # A's commit, written as the relay would: step 1, B to move.
    with rs.DB_LOCK, rs.get_db() as conn:
        state = json.loads(conn.execute("SELECT state_json FROM sessions WHERE session_id = ?", (sid,)).fetchone()[0])
        state["data"]["commitments"] = {a: "0" * 64}
        state["current_agent"], state["step_number"] = b, 1
        conn.execute("UPDATE sessions SET state_json = ?, current_agent = ?, step_number = 1, status = 'active' "
                     "WHERE session_id = ?", (json.dumps(state), b, sid))
    assert client.post(f"/sessions/{sid}/abandon", json={}, headers=_hdr(a)).status_code == 200
    stored = rs.load_session(sid)
    assert stored["state"]["metadata"]["abandoned_by"] == a
    from emporia.routes import standing as st
    st._TABLE_CACHE["key"] = None
    rows = [g for g in st._craft_games() if g["session_id"] == sid]
    assert rows and rows[0]["abandoned_by"] == a, "the one who abandoned walked away"
    assert client.get(f"/agents/{a}/sheet").json()["trust"]["abandoned_on_turn"] == 1
    assert client.get(f"/agents/{b}/sheet").json()["trust"]["abandoned_on_turn"] == 0


def test_an_idle_or_old_abandon_still_blames_whose_turn_it_was():
    from emporia.routes.standing import quitter_of
    old = {"participants": ["a", "b"], "current_agent": "b", "state": {"metadata": {}}}
    assert quitter_of(old) == "b"
    assert quitter_of({**old, "state": {"metadata": {"abandoned_by": "a"}}}) == "a"
    assert quitter_of({**old, "state": {"metadata": {"abandoned_by": "operator"}}}) == "b"
    assert quitter_of({**old, "current_agent": ""}) is None
