"""The newcomer path (2.1): `standing.next` on the sheet, `standing` on /relay/summary, and counted vs raw games
in /agents/{id}/record. No scoring changes; every field is additive, so an older reader ignores them."""
import uuid

import pytest
from fastapi.testclient import TestClient

from emporia import newcomer
from emporia import progression as pg
from emporia import relay_server as rs
from tests.test_progression import _register, _session


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    return TestClient(rs.app)


def _label(agent, label):
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET operator_label = ? WHERE agent_id = ?", (label, agent))
    rs.forget_who()


def _blotto(a, b):
    return _session("emporia:blotto:v2", [a, b], {"wins": {a: 2, b: 0}, "history": [{}, {}], "rounds": 3})


def _ids():
    return f"np_a_{uuid.uuid4().hex[:6]}", f"np_b_{uuid.uuid4().hex[:6]}"


def test_next_step_words_each_state():
    lone = newcomer.next_step(others_here=0, counted_operators=0)
    assert "No other operator is active" in lone and "visit_relay" in lone
    waiting = newcomer.next_step(others_here=3, counted_operators=0)
    assert "3 other operators are active" in waiting and "finish one public game" in waiting
    lifting = newcomer.next_step(others_here=3, counted_operators=1, tier=1, started=True,
                                 raises="finish games with 2 different operators (you have 1 of 2 for Known)")
    assert lifting.startswith("Next: finish games") and "1 of 2" in lifting
    assert newcomer.next_step(others_here=3, counted_operators=4, tier=3, started=True).startswith("Next: keep")


def test_the_lone_agents_sheet_says_what_to_do(client):
    a, _ = _ids()
    _register(a)
    sh = client.get(f"/agents/{a}/sheet").json()
    assert sh["standing"]["counting"] is False and "No other operator" in sh["standing"]["next"]
    assert "weeks_active" in sh["trust"] and sh["trust"]["started"] is False
    assert sh["levels"]["trust"] == 1 and sh["class"] is None


def test_the_lone_agent_why_line_is_a_fact_and_not_a_warning():
    who = {"me": pg.Who("op-me", 0), "mine2": pg.Who("op-me", 0)}
    like = {"agent_id": "mine2", "to_agent": "me", "kind": "post", "target_id": "p", "at": "2026-11-05T00:00:00+00:00"}
    why = pg.renown("me", [like], who)["why"]
    assert why == [pg.RENOWN_WHY["operator"]] and "your own operator count nothing" in why[0]
    assert "other operators" in why[0]


def test_relay_summary_says_why_boards_are_empty_and_how_standing_is_earned(client):
    s = client.get("/relay/summary").json()
    st = s["standing"]
    assert {"counting", "operators", "operators_active_30d", "reason", "what_counts", "earned_by"} <= set(st)
    assert len(st["what_counts"]) == 3 and "other operators" in st["earned_by"]
    assert all(k in s for k in ("agents", "operators", "open_challenges", "season"))    # the old shape stays


def test_relay_block_by_operator_count():
    assert "one operator" in newcomer.relay_block(operators_here=1, active_30d=1)["reason"]
    assert newcomer.relay_block(operators_here=1, active_30d=1)["counting"] is False
    assert newcomer.relay_block(operators_here=5, active_30d=3) == {**newcomer.relay_block(operators_here=5, active_30d=3),
                                                                    "counting": True, "reason": ""}


def test_the_record_names_why_won_games_did_not_count(client):
    a, b = _ids()
    for x in (a, b):
        _register(x)
    _label(a, "op_same_np")
    _label(b, "op_same_np")
    _blotto(a, b)
    _blotto(b, a)
    rec = client.get(f"/agents/{a}/record").json()
    assert rec["games"]["finished"] == 2 and rec["games"]["won"] >= 1          # the raw record, as before
    c = rec["counting"]
    assert c["games_counted"] == 0 and c["games_not_counted"]["own_operator"] == 2
    assert "own operator" in c["line"] and "Craft is empty" in c["line"]
    assert rec["signature"] and rec["counted"] == 2                             # old fields and signature stay


def test_a_game_with_another_operator_counts_on_the_record_line(client):
    a, b = _ids()
    for x in (a, b):
        _register(x)
    _blotto(a, b)
    c = client.get(f"/agents/{a}/record").json()["counting"]
    assert c["games_counted"] == 1 and "counts 1 of 1" in c["line"]


def test_a_record_without_the_new_block_still_reads_as_a_carried_record():
    """A 2.0.1 relay's record has no `counting`: carrying and showing it must not need it."""
    from emporia import moves
    old = {"scope": "this_relay", "agent_id": "x", "public_key_hex": "k", "games": {"finished": 3, "won": 3},
           "as_buyer": {}, "as_seller": {}, "counted": 3}
    assert moves._known_fields(old)["games"] == {"finished": 3, "won": 3}
    assert moves._known_fields({**old, "counting": {"x": 1}}).get("counting") is None
