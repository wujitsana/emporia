"""Review pass 2 (2026-11-06, gamify): proof tests for what it found and fixed.

`docs/review-pass-2026-11-06-gamify.md` lists every finding; each fix here has its test."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from emporia import pledges as PL
from emporia import relay_server as rs
from tests.test_emporia import _register
from tests.test_pledges import (  # noqa: F401
    _drop_made_sessions,
    act,
    agents,
    at,
    deal,
    make,
    pick,
    pledge,
    record_action,
    session,
    set_session,
    ts,
)


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


def test_a_tell_is_due_only_when_a_commit_is_the_makers_to_make():
    """Found: an open tell always said do=commit, so whats_next told a maker who had already
    committed (or whose turn it was not) to commit_move, which the relay then refused."""
    p = pledge("tell", {"tell": "even_split"})
    blotto = {"status": "active", "participants": ["c", "m"], "updated_at": at(0), "turn_order": True,
              "current_agent": "m", "state": {"data": {"phase": "commit"}}}
    assert PL.resolve(p, blotto, [], {}, ts(10)).get("do") == "commit"
    assert "do" not in PL.resolve(p, {**blotto, "current_agent": "c"}, [], {}, ts(10))
    assert "do" not in PL.resolve(p, {**blotto, "state": {"data": {"phase": "reveal"}}}, [], {}, ts(10))
    two = pick(status="active", phase="commit", pending=["c", "x"])
    assert "do" not in PL.resolve(p, two, [], {}, ts(10)), "m has committed; the others have not"
    assert PL.resolve(p, pick(status="active", phase="commit", pending=["m"]), [], {}, ts(10)).get("do") == "commit"
    assert PL.due("m", [{**p, **PL.resolve(p, two, [], {}, ts(10))}], ts(10)) == []


# ── what an agent reads ─────────────────────────────────────────────────────

def test_an_agent_in_a_bargain_is_told_its_moves():
    """Found: whats_next sent a bargain player to get_session "then submit_action", and
    submit_action's description named chess, service, research and code review, never
    bargain's offer/accept/decline; get_session's never said where the agent's values are."""
    from emporia import mcp_surface, next_steps
    from emporia import playbook as P
    items = next_steps.rank({"sessions": [{"session_id": "s1", "module_type": "emporia:bargain:v2", "move": "move",
                                           "clock_left_s": 400}]}, P.PRESETS["custom"])
    assert "offer" in items[0]["why"] and "accept" in items[0]["why"] and "private" in items[0]["why"]
    assert "bargain: offer" in mcp_surface.DESCRIPTIONS["submit_action"]
    assert "private" in mcp_surface.DESCRIPTIONS["get_session"]
    assert "walk-away" in mcp_surface.DESCRIPTIONS["abandon_session"]


# ── Bargain: verify_session on a game that was never dealt ──────────────────

def test_a_bargain_abandoned_before_anyone_joined_is_unchecked_not_failed(client):
    """Found: a bargain its creator abandoned while it waited for a player was never dealt,
    yet verify_session's finding said FAIL, so an honest relay's record read as tampered with."""
    from emporia.verify import verify_game
    from tests.test_emporia import _register_keyed
    a = f"r2_bg_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, a)
    r = client.post("/sessions", json={"module_type": "emporia:bargain:v2", "config": {}}, headers=_hdr(a))
    sid = r.json()["session_id"]
    assert client.post(f"/sessions/{sid}/abandon", json={}, headers=_hdr(a)).status_code == 200
    record = {"session": client.get(f"/sessions/{sid}", headers=_hdr(a)).json(),
              "log": client.get(f"/sessions/{sid}/audit", headers=_hdr(a)).json()["chain"],
              "public_keys": {a: client.get(f"/agents/{a}", headers=_hdr(a)).json()["public_key_hex"]},
              "receipts": [], "relay_key": client.get("/health").json()["relay_public_key"], "relay_verdict": None}
    deal = next(f for f in verify_game(**record)["findings"] if f["check"] == "rand")
    assert deal["status"] == "unchecked", deal


# ── pledges: a tell must say something ──────────────────────────────────────

def test_a_tell_that_rules_nothing_out_or_no_legal_move_is_refused(client):
    """Found: `pick_below 101` in guess ⅔ (picks 0–100) holds for every pick: a pledge kept
    with evidence for nothing. A declared move the game would never take could only break."""
    m, c, x = agents(client, "tm", "tc", "tx")
    sid = session("emporia:two-thirds:v2", [m, c, x], {"phase": "commit", "pending": [m, c, x], "commitments": {}})
    for vacuous in ({"tell": "pick_below", "value": 101}, {"tell": "pick_below", "value": 0},
                    {"tell": "pick_above", "value": 100}, {"tell": "pick_above", "value": -1}):
        r = make(client, m, c, "tell", sid, vacuous)
        assert r.status_code == 422 and "rules out no pick" in r.json()["detail"], (vacuous, r.text)
    assert make(client, m, c, "tell", sid, {"tell": "pick_below", "value": 34}).status_code == 200
    assert make(client, m, c, "tell", sid, {"tell": "move", "value": 500}).status_code == 422
    b = session("emporia:blotto:v2", [c, m], {"phase": "commit", "commitments": {}, "revealed": {},
                                             "battlefields": 5, "troops": 100})
    r = make(client, m, c, "tell", b, {"tell": "move", "value": [50, 50, 50, 0, 0]})
    assert r.status_code == 422 and "not a legal move" in r.json()["detail"]
    assert make(client, m, c, "tell", b, {"tell": "move", "value": [20, 20, 20, 20, 20]}).status_code == 200


# ── pledges: evidence timing and the endings that used to void a broken pledge ──

def test_a_pledge_dated_back_does_not_count_a_move_already_made():
    """Found: the cut-off was the maker's signed `at`, which the relay takes up to ten minutes
    old. A maker could commit, then pledge a tell of that very move dated a minute earlier,
    and it was kept with evidence (or likewise a reveal it had already made in time)."""
    tell = {**pledge("tell", {"tell": "move", "value": [4, 3, 3]}, made=5), "recorded_at": at(60)}
    game = {"status": "completed", "participants": ["c", "m"], "updated_at": at(500), "turn_order": True,
            "state": {"data": {"phase": "commit"}}}
    acts = [act("m", "commit", 30), act("c", "commit", 40), act("m", "reveal", 50, [4, 3, 3]),
            act("c", "reveal", 55, [5, 5, 0])]
    assert PL.resolve(tell, game, acts, {}, ts(600))["status"] == "void", "the commit came before the relay had it"
    honest = {**tell, "made_at": at(5), "recorded_at": at(6)}
    assert PL.resolve(honest, game, acts, {}, ts(600))["status"] == "kept"
    rev = {**pledge("reveal_within", {"seconds": 120}, made=5), "recorded_at": at(45)}
    commits = [act("m", "commit", 10), act("c", "commit", 20), act("x", "commit", 30)]
    done = [*commits, act("m", "reveal", 35), act("c", "reveal", 50), act("x", "reveal", 60)]
    assert PL.resolve(rev, pick(pending=[]), done, {}, ts(2000))["status"] == "void"


def test_a_seller_cannot_void_a_missed_deadline_by_abandoning_the_deal():
    p = pledge("deliver_by", {"by": at(3600)})
    late = deal(status="abandoned", updated=4000)
    assert PL.resolve(p, late, [], {}, ts(5000))["status"] == "broken", "called off after the deadline had passed"
    base = deal(status="abandoned", updated=100)
    by_seller = {**base, "state": {**base["state"], "metadata": {"abandoned_by": "m"}}}
    assert PL.resolve(p, by_seller, [], {}, ts(200))["status"] == "broken", "the seller walked away from its word"
    by_buyer = {**base, "state": {**base["state"], "metadata": {"abandoned_by": "c"}}}
    assert PL.resolve(p, by_buyer, [], {}, ts(200))["status"] == "void", "the buyer called it off in time"


def test_a_reveal_never_made_is_broken_even_before_the_pledged_deadline():
    """Found: a maker who pledged reveal_within longer than the game's own clock, then never
    revealed (lost on time, or abandoned its own turn), got `void`: the game ended before the
    pledged deadline. It never revealed, so it broke its word."""
    p = pledge("reveal_within", {"seconds": 3600}, made=5)
    commits = [act("m", "commit", 10), act("c", "commit", 20), act("x", "commit", 30)]
    on_time_loss = pick(pending=["m"], updated=640)          # the 600 s phase clock ran out
    assert PL.resolve(p, on_time_loss, commits, {}, ts(700))["status"] == "broken"
    walked = {**pick(status="abandoned", pending=["m"], updated=100),
              "state": {"data": {"phase": "reveal", "pending": ["m"]}, "metadata": {"abandoned_by": "m"}}}
    assert PL.resolve(p, walked, commits, {}, ts(200))["status"] == "broken"
    other = {**walked, "state": {**walked["state"], "metadata": {"abandoned_by": "c"}}}
    assert PL.resolve(p, other, commits, {}, ts(200))["status"] == "void", "another player ended it"
