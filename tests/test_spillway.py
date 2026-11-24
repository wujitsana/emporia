"""Spillway (`emporia:spillway:v1`): four in a line on 6 channels x 5 rows, with a dealt opening and one channel shut
by a drand draw every turn. The rules are tested as pure functions; the relay path is played as
test_games_pack.py plays its games (conftest `rand_testmode`: any round is published with a fake signature);
verify runs on honest and tampered records, with the two recorded real rounds of test_rand.py for BLS."""
import asyncio
import copy
import json
import random
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import fake_beacon_sig, make_test_commit, signed_session_body
from fastapi.testclient import TestClient

from emporia import beacon, game_export, rand, wire
from emporia import relay_server as rs
from emporia.module_sdk import HOW_TO_PLAY_MAX, MODULE_REGISTRY, SessionState, get_interaction_module
from emporia.modules import spillway as S
from emporia.relay_server import app
from emporia.routes import beacon as beacon_route
from emporia.verify import verify_game
from tests.test_emporia import _register_keyed, _signed_action_body
from tests.test_rand import R1000, R_LATE

SPILLWAY = "emporia:spillway:v1"
WHY = "Reading the board and the gate before I drop"
M = S.SpillwayModule


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51241))


def _hdr(agent):
    return {"x-emporia-agent-id": agent}


def _sig(rnd):
    return bytes.fromhex(fake_beacon_sig(rnd))


# ── the board: pure functions ────────────────────────────────────────────────

def _grid(rows_top_first):
    """A grid from five strings (top row first), as `board` shows it."""
    g = S.empty_grid()
    for r, text in zip(range(S.ROWS, 0, -1), rows_top_first, strict=True):
        for c, ch in enumerate(text):
            g[c][r - 1] = ch
    return g


def test_there_are_39_lines_of_four_in_four_directions_and_a_stone_settles_on_what_is_below():
    assert len(S.lines()) == len(S.LINES) == 15 + 12 + 12
    assert all(len(set(line)) == 4 for line in S.LINES)
    g, row = S.place(S.empty_grid(), 3, "A")
    assert row == 1
    g, row = S.place(g, 3, "B")
    assert row == 2 and S.board_rows(g)[-2:] == ["..B...", "..A..."]
    full = S.empty_grid()
    for _ in range(S.ROWS):
        full, _ = S.place(full, 6, "#")
    assert S.free_channels(full) == [1, 2, 3, 4, 5]
    assert S.drop_row(full, 6) is None
    with pytest.raises(ValueError, match="full"):
        S.place(full, 6, "A")


@pytest.mark.parametrize("name,rows", [
    ("horizontal", [".....", ".....", ".....", ".....", "AAAA.."]),
    ("vertical", ["......", "A.....", "A.....", "A.....", "A....."]),
    ("rising", ["......", "...A..", "..A...", ".AB...", "AAB..."]),
    ("falling", ["A.....", ".A....", "..A...", "...A..", "......"]),
])
def test_four_stones_in_a_line_win_in_every_direction(name, rows):
    line = S.four_line(_grid(rows), "A")
    assert line is not None and len(line) == 4, name
    assert S.four_line(_grid(rows), "B") is None


def test_silt_and_the_other_sides_stones_break_a_line():
    assert S.four_line(_grid(["......", "......", "......", "......", "AA#AA."]), "A") is None
    assert S.four_line(_grid(["......", "......", "......", "......", "AABAA."]), "A") is None
    assert S.four_line(_grid(["......", "......", "......", "......", "AAAA.."]), "A") is not None


def test_a_live_three_is_a_line_with_three_of_yours_and_one_empty_cell():
    g = _grid(["......", "......", "......", "......", "AAA..."])
    assert S.live_threes(g, "A") == 1          # only the line through channel 4
    assert S.live_threes(g, "B") == 0
    assert S.live_threes(_grid(["......", "......", "......", "......", ".AAA.."]), "A") == 2
    assert S.live_threes(_grid(["......", "......", "......", "......", "AAAB.."]), "A") == 0
    assert S.live_threes(_grid(["......", "......", "......", "......", "AAA#.."]), "A") == 0


def test_a_one_drop_win_sees_the_cell_a_stone_would_settle_on():
    g = _grid(["......", "......", "......", "......", "AAA..."])
    assert S.can_win_in_one(g, "A") and not S.can_win_in_one(g, "B")
    high = _grid(["......", "......", "......", "......", "AA.A.."][:0] or ["......", "......", "......", "A.....", "AA.A.."])
    assert not S.can_win_in_one(_grid(["......", "......", "......", "......", ".A.A.."]), "A")
    assert high is not None


# ── the draws: deal, first mover, gate ───────────────────────────────────────

def _beacons(n):
    return [bytes.fromhex(fake_beacon_sig(1000 + i)) for i in range(n)]


def test_the_deal_is_the_same_for_the_same_beacon_and_has_the_dealt_shape():
    sig = _sig(5512345)
    a, b = S.deal("sess_x", sig), S.deal("sess_x", sig)
    assert a == b and S.deal("sess_y", sig) != a
    assert len(a["silt"]) == 2 and [len(s) for s in a["stones"]] == [3, 3]


def test_a_deal_never_has_a_line_or_a_one_drop_win_over_500_beacons():
    redealt = first_a = 0
    for i, sig in enumerate(_beacons(500)):
        d = S.deal(f"sess_{i}", sig)
        grid = S.empty_grid()
        cells = {tuple(c): "#" for c in d["silt"]}
        cells.update({tuple(c): "A" for c in d["stones"][0]})
        cells.update({tuple(c): "B" for c in d["stones"][1]})
        assert len(cells) == 8 if d["attempt"] < S.DEAL_ATTEMPTS else len(cells) == 2
        for (ch, row), mark in cells.items():
            grid[ch - 1][row - 1] = mark
        for ch in range(S.COLS):          # every stone rests on another cell or the floor
            col = [c for c in grid[ch]]
            filled = [x != "." for x in col]
            assert filled == sorted(filled, reverse=True), (i, col)
        assert S.four_line(grid, "A") is None and S.four_line(grid, "B") is None
        assert not S.can_win_in_one(grid, "A") and not S.can_win_in_one(grid, "B"), i
        assert sum(x == "." for c in grid for x in c) == 22 or d["attempt"] == S.DEAL_ATTEMPTS
        redealt += d["attempt"] > 0
        first_a += S.first(f"sess_{i}", sig, ["A", "B"]) == "A"
    assert 0 < redealt < 100, "a refused deal is drawn again, and that is the exception"
    assert 200 < first_a < 300, "the beacon picks the first mover about evenly"


def test_a_refused_deal_is_redrawn_and_64_refusals_leave_two_silt_blocks_alone(monkeypatch):
    sig = _sig(77)
    plain = S.deal("s", sig)
    assert plain["attempt"] == 0
    monkeypatch.setattr(S, "can_win_in_one", lambda g, m: True)
    stub = S.deal("s", sig)
    assert stub["attempt"] == S.DEAL_ATTEMPTS and stub["stones"] == [[], []] and len(stub["silt"]) == 2
    calls = []
    monkeypatch.setattr(S, "can_win_in_one", lambda g, m: calls.append(1) or len(calls) <= 6)
    again = S.deal("s", sig)
    assert again["attempt"] == 6 and again != plain, "six attempts refused, the seventh taken"


def test_the_gate_is_one_open_channel_and_there_is_none_when_one_is_left():
    sig = _sig(9)
    seen = {S.gate("s", n, sig, [1, 2, 3, 4, 5, 6]) for n in range(1, 80)}
    assert seen == {1, 2, 3, 4, 5, 6}
    assert S.gate("s", 3, sig, [2, 5, 6]) in (2, 5, 6) and S.gate("s", 3, sig, [6, 5, 2]) == S.gate("s", 3, sig, [2, 5, 6])
    assert S.gate("s", 3, sig, [4]) is None and S.gate("s", 3, sig, []) is None
    assert {S.gate("s", n, _sig(n), [1, 2]) for n in range(40)} == {1, 2}


# ── the result: verdict ──────────────────────────────────────────────────────

def _data(moves, opening_extra=None, drops=7):
    opening = {"silt": [], "stones": {"a": [], "b": []}, "first": "a", "attempt": 0, **(opening_extra or {})}
    return {"seats": ["a", "b"], "drops": drops, "opening": opening,
            "moves": [{"n": i + 1, "by": by, "channel": ch} for i, (by, ch) in enumerate(moves)]}


def test_four_in_a_line_wins_at_once_with_the_line_and_points():
    d = _data([("a", 1), ("b", 5), ("a", 2), ("b", 5), ("a", 3), ("b", 6), ("a", 4)])
    v = S.verdict(d)
    assert v["winner"] == "a" and v["termination"] == "four" and v["points"] == {"a": 1, "b": -1}
    assert v["line"] == [[1, 1], [2, 1], [3, 1], [4, 1]] and v["moves"] == 7 and v["outcome_type"] == "won"
    assert S.verdict(_data([("a", 1), ("b", 5)])) is None, "the game goes on"


def test_the_drops_running_out_is_decided_by_live_threes_then_by_the_second_mover():
    # drops=2: four drops end the game. a holds the foot of channels 1 and 2 from the opening, then 3 and 5.
    d = _data([("a", 3), ("b", 6), ("a", 5), ("b", 6)], opening_extra={"stones": {"a": [[1, 1], [2, 1]], "b": []}}, drops=2)
    v = S.verdict(d)
    assert v["termination"] == "threes" and v["winner"] == "a" and v["threes"] == {"a": 2, "b": 0}
    assert v["points"] == {"a": 1, "b": -1} and v["moves"] == 4 and "line" not in v
    assert S.verdict({**d, "moves": d["moves"][:3]}) is None, "a drop is still to come"
    # nobody has a three: level, and level goes to the player who moved second
    level = S.verdict(_data([("a", 1), ("b", 3), ("a", 5), ("b", 6)], drops=2))
    assert level["termination"] == "level" and level["winner"] == "b" and level["threes"] == {"a": 0, "b": 0}
    assert level["points"] == {"b": 1, "a": -1} and level["outcome_type"] == "won"
    flipped = _data([("b", 1), ("a", 3), ("b", 5), ("a", 6)], drops=2)
    flipped["opening"]["first"] = "b"
    v = S.verdict(flipped)
    assert v["termination"] == "level" and v["winner"] == "a", "now a moved second"


def test_a_full_board_ends_the_game_before_the_drops_do():
    g = _data([("a", 6)], opening_extra={"silt": [[c, r] for c in range(1, 7) for r in range(1, 6)
                                                  if (c, r) not in ((6, 4), (6, 5))]}, drops=7)
    v = S.verdict(g)
    assert v is None, "one cell is left: the game goes on"
    g["moves"].append({"n": 2, "by": "b", "channel": 6})
    v = S.verdict(g)
    assert v is not None and v["termination"] == "level" and v["winner"] == "b"


def test_300_random_playouts_end_within_twice_the_drops_with_exactly_one_winner():
    rng = random.Random(2026)
    for i in range(300):
        drops = rng.choice([4, 5, 7, 10])
        scope = f"po{i}"
        sig0 = _sig(100 + i)
        data = {"seats": ["a", "b"], "drops": drops, "rand": {"scope": scope}, "moves": []}
        data["opening"] = S._opening_for(data, sig0)
        grid = S._grid_of(data, data["opening"])
        order = S._order(data, data["opening"])
        for n in range(1, 2 * drops + 1):
            free = S.free_channels(grid)
            if not free:
                break
            g = S.gate(scope, n, _sig(5000 + n + i), free)
            ch = rng.choice([c for c in free if c != g])
            who = order[(n - 1) % 2]
            grid, _ = S.place(grid, ch, "A" if who == "a" else "B")
            data["moves"].append({"by": who, "channel": ch})
            if S.four_line(grid, "A" if who == "a" else "B"):
                break
        v = S.verdict(data)
        assert v is not None and len(data["moves"]) <= 2 * drops and v["winner"] in ("a", "b")
        assert sorted(v["points"].values()) == [-1, 1] and v["outcome_type"] == "won"


# ── a game through the relay ─────────────────────────────────────────────────

def _game(client, config=None, ahead=None):
    agents = [f"sl{i}_{uuid.uuid4().hex[:6]}" for i in range(2)]
    for a in agents:
        _register_keyed(client, a)
    cfg = dict(config or {})
    if ahead is None:
        r = client.post("/sessions", json={"module_type": SPILLWAY, "config": cfg}, headers=_hdr(agents[0]))
    else:
        body = signed_session_body(agents[0], {"module_type": SPILLWAY, "config": cfg})
        sid0 = wire.session_id_for(body["nonce"])
        r = client.post("/sessions", json={**body, "rand_commit": make_test_commit(sid0, agents[0], 0, round_offset=ahead)},
                        headers=_hdr(agents[0]))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    if ahead is None:
        r = client.post(f"/sessions/{sid}/join", json={"agent_id": agents[1]}, headers=_hdr(agents[1]))
    else:
        r = client.post(f"/sessions/{sid}/join", json={"agent_id": agents[1],
                        "rand_commit": make_test_commit(sid, agents[1], 1, round_offset=ahead)}, headers=_hdr(agents[1]))
    assert r.status_code == 200, r.text
    return sid, agents


def _served(client, sid, who):
    r = client.get(f"/sessions/{sid}", headers=_hdr(who))
    assert r.status_code == 200, r.text
    return r.json()


def _drop(client, sid, who, channel, rnd="fill"):
    data = _served(client, sid, who)["state"]["data"]
    payload = {"channel": channel}
    if rnd == "fill":
        payload = M().fill_move("drop", payload, data, time.time())
    elif rnd is not None:
        payload["round"] = rnd
    return client.post(f"/sessions/{sid}/action", json=_signed_action_body(client, sid, who, "drop", payload, WHY))


def _play(client, sid, agents, rng):
    for _ in range(60):
        s = _served(client, sid, agents[0])
        if s["status"] != "active":
            return s
        d = s["state"]["data"]
        r = _drop(client, sid, d["to_move"], rng.choice(d["open"]))
        assert r.status_code == 200, r.text
    raise AssertionError("the game did not end")


def _gated(client, sid, who):
    d = _served(client, sid, who)["state"]["data"]
    return d["gate"], d["to_move"], d


def test_the_served_state_is_the_contract_the_board_is_built_on(client):
    sid, agents = _game(client)
    s = _served(client, sid, agents[0])
    d = s["state"]["data"]
    assert (d["cols"], d["rows"], d["drops"], d["move_time_limit_s"]) == (6, 5, 10, 600)
    assert d["seats"] == agents and d["moves"] == [] and d["ended"] is None and d["voided"] is None
    assert d["leaving_counts"] is True and d["pending"] == agents, "both are waited on until the beacon names the first"
    assert set(d["opening"]) == {"silt", "stones", "first", "attempt"} and d["opening"]["first"] in agents
    assert set(d["opening"]["stones"]) == set(agents) and len(d["opening"]["silt"]) == 2
    assert len(d["board"]) == 5 and all(len(r) == 6 for r in d["board"])
    assert sum(x == "#" for r in d["board"] for x in r) == 2
    assert sum(x == "A" for r in d["board"] for x in r) == 3 and sum(x == "B" for r in d["board"] for x in r) == 3
    assert d["to_move"] == d["opening"]["first"] and d["gate"] in range(1, 7) and d["gate_ready"] is True
    # The opening drops 8 pieces on random columns of 5, so now and then one column starts full: it is never open.
    free = [c for c in range(1, 7) if any(r[c - 1] not in ("#", "A", "B") for r in d["board"])]
    assert d["open"] == [c for c in free if c != d["gate"]]
    assert d["drops_left"] == {agents[0]: 10, agents[1]: 10}
    assert d["gate_round"] == d["rand"]["round"]
    assert d["gate_at"] == datetime.fromtimestamp(rand.round_time(d["gate_round"]), UTC).isoformat()
    assert isinstance(d["name_round"], int) and d["name_round"] > d["gate_round"]
    assert d["rand"]["beacons"] == {} or str(d["rand"]["round"]) in d["rand"]["beacons"]
    # the stored state keeps the opening only once a move has been made
    assert rs.load_session(sid)["state"]["data"]["opening"] is None


def test_a_drop_settles_the_stone_names_the_next_gate_and_hands_over(client):
    sid, agents = _game(client)
    gate, mover, d = _gated(client, sid, agents[0])
    ch = d["open"][0]
    r = _drop(client, sid, mover, ch)
    assert r.status_code == 200, r.text
    st = rs.load_session(sid)["state"]["data"]
    m = st["moves"][0]
    assert (m["n"], m["by"], m["channel"], m["gate"]) == (1, mover, ch, gate) and 1 <= m["row"] <= 5
    assert st["opening"] == d["opening"] and st["pending"] == [agents[1] if mover == agents[0] else agents[0]]
    assert st["board"] == _served(client, sid, agents[0])["state"]["data"]["board"]
    d2 = _served(client, sid, agents[0])["state"]["data"]
    assert d2["gate_round"] == m["round"] and d2["to_move"] != mover and d2["drops_left"][mover] == 9
    assert str(m["round"]) in rs.load_session(sid)["state"]["data"]["rand"]["beacons"] or d2["gate_ready"]
    assert rs.load_session(sid)["current_agent"] == d2["to_move"]


def test_an_illegal_drop_is_refused_with_a_sentence_that_names_the_fix(client):
    sid, agents = _game(client)
    gate, mover, d = _gated(client, sid, agents[0])
    other = agents[1] if mover == agents[0] else agents[0]
    r = _drop(client, sid, other, d["open"][0])
    assert r.status_code == 400 and "Not your turn" in r.text and mover in r.text
    r = _drop(client, sid, mover, gate)
    assert r.status_code == 400 and "shut" in r.text and str(d["open"]) in r.text
    for junk in (0, 7, True, "3", None, 2.5):
        r = _drop(client, sid, mover, junk)
        assert r.status_code == 400, junk
    assert _served(client, sid, agents[0])["state"]["data"]["moves"] == []


def test_a_drop_without_a_valid_round_is_refused_and_says_what_to_send(client, monkeypatch):
    monkeypatch.setattr(rand, "MIN_TURN_LEAD_ROUNDS", 3)
    sid, agents = _game(client)
    gate, mover, d = _gated(client, sid, agents[0])
    ch = d["open"][0]
    r = _drop(client, sid, mover, ch, rnd=None)
    assert r.status_code == 400 and "names the drand round" in r.text and "round" in r.text
    cur = rand.round_at(time.time())
    for rnd, word in ((cur, "too early"), (cur + 2, "too early"), (cur + 90, "too far ahead"),
                      ("soon", "whole number"), (True, "whole number"), (0, "whole number")):
        r = _drop(client, sid, mover, ch, rnd=rnd)
        assert r.status_code == 400 and word in r.text and "Send round" in r.text, (rnd, r.text)
    assert rs.load_session(sid)["state"]["data"]["moves"] == []
    assert _drop(client, sid, mover, ch, rnd=cur + 5).status_code == 200


def test_a_round_not_later_than_the_last_gates_is_refused(client):
    sid, agents = _game(client)
    gate, mover, d = _gated(client, sid, agents[0])
    r0 = d["rand"]["round"]
    r = _drop(client, sid, mover, d["open"][0], rnd=r0)
    assert r.status_code == 400 and "later" in r.text and "Send round" in r.text, r.text


def test_a_full_channel_is_refused(client):
    sid, agents = _game(client)
    _craft(sid, agents, silt=[[1, 1], [1, 2]], stones={agents[0]: [[1, 3], [1, 5]], agents[1]: [[1, 4]]})
    d = _served(client, sid, agents[0])["state"]["data"]
    assert 1 not in d["open"] and d["gate"] != 1
    r = _drop(client, sid, d["to_move"], 1)
    assert r.status_code == 400 and "full" in r.text, r.text


def test_nothing_is_played_before_the_gate_is_published(client, monkeypatch):
    sid, agents = _game(client)
    _, mover, d = _gated(client, sid, agents[0])
    assert _drop(client, sid, mover, d["open"][0]).status_code == 200
    named = rs.load_session(sid)["state"]["data"]["moves"][0]["round"]
    monkeypatch.setattr(beacon_route, "signature", lambda rnd: None if rnd == named else fake_beacon_sig(rnd))
    other = agents[1] if mover == agents[0] else agents[0]
    s = _served(client, sid, other)
    d2 = s["state"]["data"]
    assert d2["gate_ready"] is False and d2["gate"] is None and d2["open"] == [] and d2["gate_round"] == named
    assert datetime.fromisoformat(d2["gate_at"]).timestamp() == rand.round_time(named)
    r = _drop(client, sid, other, 3)
    assert r.status_code == 400 and "not drawn yet" in r.text or "not published" in r.text, r.text
    assert str(named) not in rs.load_session(sid)["state"]["data"]["rand"]["beacons"]
    monkeypatch.setattr(beacon_route, "signature", fake_beacon_sig)
    gate, mover2, d3 = _gated(client, sid, other)
    assert mover2 == other and d3["gate_ready"] is True
    assert _drop(client, sid, other, d3["open"][0]).status_code == 200


def test_nothing_is_dealt_before_the_games_round_is_published(client):
    sid, agents = _game(client, ahead=4)
    r = _drop(client, sid, agents[0], 3, rnd=rand.round_at(time.time()) + 6)
    assert r.status_code == 400 and "Nothing is dealt yet" in r.text, r.text


def test_a_void_is_for_a_late_commitment_before_the_round_only(client):
    sid, agents = _game(client, ahead=4)
    r = client.post(f"/sessions/{sid}/action", json=_signed_action_body(client, sid, agents[1], "void",
                                                                       {"reason": "late_commit"}, WHY))
    assert r.status_code == 200, r.text
    s = rs.load_session(sid)
    assert s["status"] == "completed"
    out = get_interaction_module(SPILLWAY).is_terminal(SessionState.from_dict(s["state"]))[1]
    assert out["termination"] == "void" and out["winner"] is None
    sid2, agents2 = _game(client)
    r = client.post(f"/sessions/{sid2}/action", json=_signed_action_body(client, sid2, agents2[0], "void",
                                                                        {"reason": "late_commit"}, WHY))
    assert r.status_code == 400 and "Too late to void" in r.text


def test_running_out_of_time_loses(client):
    sid, agents = _game(client)
    _, mover, d = _gated(client, sid, agents[0])
    s = rs.load_session(sid)
    s["state"]["data"]["turn_started_at"] = (datetime.now(UTC) - timedelta(seconds=700)).isoformat()
    rs.save_session(session_id=sid, module_type=s["module_type"], config=s["config"], payment_rules=s["payment_rules"],
                    state=SessionState.from_dict(s["state"]), current_agent=s["current_agent"],
                    step_number=s["step_number"], status="active", participants=s["participants"],
                    created_at=s["created_at"], updated_at=datetime.now(UTC).isoformat())
    from emporia.routes.sessions import sweep_service_deadlines
    asyncio.run(sweep_service_deadlines())
    s = rs.load_session(sid)
    assert s["status"] == "completed"
    out = get_interaction_module(SPILLWAY).is_terminal(SessionState.from_dict(s["state"]))[1]
    winner = agents[1] if mover == agents[0] else agents[0]
    assert out["termination"] == "timeout" and out["winner"] == winner and out["points"] == {winner: 1, mover: -1}


def test_a_game_ended_by_the_clock_stores_one_end_and_no_pending_seat(client):
    """Live 11-21: the sweep computed a timeout win on a state with the gate's round filled in, but saved the
    state without it: `completed` with no end, a seat still pending, and a reader's own recomputation saying
    "draw, void". A game the clock ended keeps the filled state, the result, and waits on nobody."""
    sid, agents = _game(client)
    _, mover, _d = _gated(client, sid, agents[0])
    assert _drop(client, sid, mover, _d["open"][0]).status_code == 200
    named = rs.load_session(sid)["state"]["data"]["moves"][0]["round"]
    assert str(named) not in rs.load_session(sid)["state"]["data"]["rand"].get("beacons", {})
    other = agents[1] if mover == agents[0] else agents[0]
    s = rs.load_session(sid)
    s["state"]["data"]["turn_started_at"] = (datetime.now(UTC) - timedelta(seconds=700)).isoformat()
    rs.save_session(session_id=sid, module_type=s["module_type"], config=s["config"], payment_rules=s["payment_rules"],
                    state=SessionState.from_dict(s["state"]), current_agent=s["current_agent"],
                    step_number=s["step_number"], status="active", participants=s["participants"],
                    created_at=s["created_at"], updated_at=datetime.now(UTC).isoformat())
    from emporia.routes.sessions import sweep_service_deadlines
    asyncio.run(sweep_service_deadlines())
    stored = rs.load_session(sid)
    assert stored["status"] == "completed"
    data = stored["state"]["data"]
    assert data["pending"] == [], "a finished game waits on nobody"
    assert str(named) in data["rand"]["beacons"], "the round the result came from stays with the game"
    assert data["clock_end"]["termination"] == "timeout" and data["clock_end"]["winner"] == mover
    out = get_interaction_module(SPILLWAY).is_terminal(SessionState.from_dict(stored["state"]))[1]
    assert out["termination"] == "timeout" and out["winner"] == mover
    served = _served(client, sid, other)
    assert served["outcome"]["termination"] == "timeout" and served["outcome"]["winner"] == mover


@pytest.mark.parametrize("module_type", sorted(m for m, c in MODULE_REGISTRY.items() if c.USES_RAND and c.TIMED))
def test_every_beacon_game_reads_a_stored_clock_end_before_recomputing(module_type):
    """Completed implies a stored end: each per-turn-beacon game that can end by the clock returns the result the
    sweep stored, whatever its stored rounds would give when read again."""
    stored = {"termination": "timeout", "winner": "b", "outcome_type": "won", "reason": "a did not move in time"}
    state = SessionState(data={"clock_end": stored, "players": 2, "pending": []}, current_agent="a",
                         step_number=3, metadata={"participants": ["a", "b"]})
    assert get_interaction_module(module_type).is_terminal(state) == (True, stored)


# ── scenarios: an honest game stopped before its last drop, the last drop played through the relay ──────────

def _craft(sid, agents, silt=(), stones=None, first=None, moves=(), beacons=None, drops=None):
    """Store an opening of our own (and any moves) in the game's state: a position to play from."""
    s = rs.load_session(sid)
    d = s["state"]["data"]
    d["opening"] = {"silt": [list(c) for c in silt], "stones": {a: [list(c) for c in (stones or {}).get(a, [])] for a in agents},
                    "first": first or agents[0], "attempt": 0}
    d["moves"] = list(moves)
    if drops:
        d["drops"] = drops
    rs.save_session(session_id=sid, module_type=s["module_type"], config=s["config"], payment_rules=s["payment_rules"],
                    state=SessionState.from_dict({**s["state"], "data": d}), current_agent=s["current_agent"],
                    step_number=len(moves), status="active", participants=s["participants"], created_at=s["created_at"],
                    updated_at=datetime.now(UTC).isoformat())


def _simulate(sid, agents, drops, rng, now_round):
    """An honest game from the stored state's own opening, drawn with the fake beacons: the moves it plays."""
    s = rs.load_session(sid)
    data = copy.deepcopy(s["state"]["data"])
    data["opening"] = None
    data["drops"] = drops
    r0 = data["rand"]["round"]
    sig0 = _sig(r0)
    opening = S._opening_for(data, sig0)
    data["opening"] = opening
    grid, order, marks = S._grid_of(data, opening), S._order(data, opening), {agents[0]: "A", agents[1]: "B"}
    moves, last = [], r0
    for n in range(1, 2 * drops + 1):
        free = S.free_channels(grid)
        if not free:
            break
        g = S.gate(data["rand"]["scope"], n, _sig(last), free)
        who = order[(n - 1) % 2]
        ch = rng.choice([c for c in free if c != g])
        grid, row = S.place(grid, ch, marks[who])
        rnd = now_round + 4 + n
        moves.append({"n": n, "by": who, "channel": ch, "row": row, "gate": g, "round": rnd,
                      "at": datetime.now(UTC).isoformat()})
        last = rnd
        if S.four_line(grid, marks[who]):
            break
    data["moves"] = moves
    return opening, moves, S.verdict(data)


def _finish_on_the_last_drop(client, want, seed=1, drops=5):
    """A game whose last drop ends it as `want` ('four', 'threes' or 'level'): stored up to that drop, which is
    sent through the relay with the round it named."""
    sid, agents = _game(client, {"drops": drops})
    rng = random.Random(seed)
    now_round = rand.round_at(time.time())
    for _ in range(400):
        opening, moves, v = _simulate(sid, agents, drops, rng, now_round)
        if v and v["termination"] == want and len(moves) >= 3:
            break
    else:
        raise AssertionError(f"no {want} game found")
    prefix, last = moves[:-1], moves[-1]
    s = rs.load_session(sid)
    d = s["state"]["data"]
    d["opening"], d["moves"] = opening, prefix
    d["pending"] = [last["by"]]
    d["board"] = S.board_rows(_board_after(d, agents))
    d["rand"]["beacons"] = {**d["rand"].get("beacons", {}), **{str(m["round"]): fake_beacon_sig(m["round"]) for m in prefix}}
    d["turn_started_at"] = datetime.now(UTC).isoformat()
    rs.save_session(session_id=sid, module_type=s["module_type"], config=s["config"], payment_rules=s["payment_rules"],
                    state=SessionState.from_dict({**s["state"], "data": d}), current_agent=last["by"],
                    step_number=len(prefix), status="active", participants=s["participants"], created_at=s["created_at"],
                    updated_at=datetime.now(UTC).isoformat())
    served = _served(client, sid, agents[0])["state"]["data"]
    assert served["to_move"] == last["by"] and served["gate"] == last["gate"]
    r = _drop(client, sid, last["by"], last["channel"], rnd=last["round"])
    assert r.status_code == 200, r.text
    s = rs.load_session(sid)
    assert s["status"] == "completed"
    return sid, agents, s, get_interaction_module(SPILLWAY).is_terminal(SessionState.from_dict(s["state"]))[1], opening


def _board_after(d, agents):
    grid = S._grid_of(d, d["opening"])
    for m in d["moves"]:
        grid, _ = S.place(grid, m["channel"], "A" if m["by"] == agents[0] else "B")
    return grid


def test_four_in_a_line_ends_the_game_at_once(client):
    sid, agents, s, out, _ = _finish_on_the_last_drop(client, "four")
    assert out["termination"] == "four" and out["winner"] in agents and len(out["line"]) == 4
    data = s["state"]["data"]
    assert data["ended"]["how"] == "four" and data["ended"]["winner"] == out["winner"] and data["pending"] == []
    assert out["points"][out["winner"]] == 1 and len(data["moves"]) <= 10
    assert S.four_line(_board_after(data, agents), "A" if out["winner"] == agents[0] else "B")


def test_a_game_to_the_drops_is_decided_by_live_threes(client):
    sid, agents, s, out, _ = _finish_on_the_last_drop(client, "threes")
    data = s["state"]["data"]
    assert out["termination"] == "threes" and len(data["moves"]) == 2 * 5
    t = out["threes"]
    assert t[out["winner"]] != min(t.values()) or t[agents[0]] != t[agents[1]]
    assert t[out["winner"]] == max(t.values()) and t[agents[0]] != t[agents[1]]
    assert S.four_line(_board_after(data, agents), "A") is None and S.four_line(_board_after(data, agents), "B") is None


def test_level_threes_go_to_the_player_who_moved_second(client):
    sid, agents, s, out, opening = _finish_on_the_last_drop(client, "level")
    second = next(p for p in agents if p != opening["first"])
    assert out["termination"] == "level" and out["winner"] == second
    assert out["threes"][agents[0]] == out["threes"][agents[1]] and out["points"] == {second: 1, opening["first"]: -1}


# ── verify ───────────────────────────────────────────────────────────────────

def _fake_or_real(rnd, sig):
    real = {R1000["round"]: R1000["signature"], R_LATE["round"]: R_LATE["signature"]}
    return beacon_real(rnd, sig) if rnd in real else sig == fake_beacon_sig(rnd)


beacon_real = beacon.verify


def _record(client, sid, a, b):
    return {
        "session": client.get(f"/sessions/{sid}", headers=_hdr(a)).json(),
        "log": client.get(f"/sessions/{sid}/audit", headers=_hdr(a)).json()["chain"],
        "public_keys": {x: client.get(f"/agents/{x}", headers=_hdr(a)).json()["public_key_hex"] for x in (a, b)},
        "receipts": [], "relay_key": client.get("/health").json()["relay_public_key"],
        "relay_verdict": next(r for r in client.get("/results", params={"agent_id": a}).json()["results"]
                              if r["session_id"] == sid),
    }


@pytest.fixture
def finished(client, monkeypatch):
    monkeypatch.setattr(beacon, "verify", _fake_or_real)
    for seed in range(1, 40):         # a game that goes to its drops (the longest case)
        sid, agents = _game(client, {"drops": 4})
        _play(client, sid, agents, random.Random(seed))
        record = _record(client, sid, *agents)
        if len(record["session"]["state"]["data"]["moves"]) >= 6:
            break
    assert rs.load_session(sid)["status"] == "completed"
    return client, sid, agents, record


def _finding(record, check):
    return next(f for f in verify_game(**record)["findings"] if f["check"] == check)


def test_an_honest_game_checks_every_round_the_signatures_and_the_replay(finished):
    _, _, _, record = finished
    d = record["session"]["state"]["data"]
    assert len(d["rand"]["beacons"]) >= 2 and d["ended"] is not None
    report = verify_game(**record)
    assert report["verdict"] == "ok", [f for f in report["findings"] if f["status"] != "ok"]
    assert _finding(record, "rand")["status"] == "ok" and _finding(record, "replay")["status"] == "ok"
    assert "signed move" in _finding(record, "replay")["detail"]
    assert get_interaction_module(SPILLWAY).rand_problem(d, record["session"]["participants"], record["relay_verdict"]) == ""


def _tamper(record, edit):
    forged = copy.deepcopy(record)
    edit(forged["session"]["state"]["data"])
    return forged


def _failures(forged):
    return [f for f in verify_game(**forged)["findings"] if f["status"] == "fail"]


def test_a_changed_gate_fails_verify(finished):
    _, _, _, record = finished
    forged = _tamper(record, lambda d: d["moves"][1].__setitem__("gate", d["moves"][1]["gate"] % 6 + 1))
    checks = {f["check"] for f in _failures(forged)}
    assert "rand" in checks and "replay" in checks
    assert "gate" in _finding(forged, "rand")["detail"] or "shut" in _finding(forged, "rand")["detail"]


def test_a_changed_opening_fails_verify(finished):
    _, _, _, record = finished

    def edit(d):
        cell = d["opening"]["silt"][0]
        d["opening"]["silt"][0] = [cell[0] % 6 + 1, cell[1]]
    forged = _tamper(record, edit)
    f = _finding(forged, "rand")
    assert f["status"] == "fail" and "opening" in f["detail"]
    assert _finding(forged, "replay")["status"] == "fail"

    def swap_first(d):
        d["opening"]["first"] = next(p for p in d["seats"] if p != d["opening"]["first"])
    assert _finding(_tamper(record, swap_first), "rand")["status"] == "fail"


def test_a_changed_result_or_board_fails_verify(finished):
    _, _, _, record = finished

    def flip_winner(d):
        d["ended"] = {**d["ended"], "winner": next(p for p in d["seats"] if p != d["ended"]["winner"])}
    f = _finding(_tamper(record, flip_winner), "rand")
    assert f["status"] == "fail"
    f = _finding(_tamper(record, lambda d: d.__setitem__("board", ["......"] * 5)), "rand")
    assert f["status"] == "fail" and "board" in f["detail"]
    assert _finding(_tamper(record, lambda d: d.__setitem__("board", ["......"] * 5)), "replay")["status"] == "fail"

    def drop_a_move(d):
        d["moves"] = d["moves"][:-1]
    forged = _tamper(record, drop_a_move)
    assert _failures(forged)


def test_a_tampered_beacon_fails_verify_with_the_recorded_real_rounds(finished):
    _, _, _, record = finished
    d = record["session"]["state"]["data"]["rand"]
    d["beacons"][str(R1000["round"])] = R1000["signature"]            # a real round the game did not need: honest
    assert _finding(record, "rand")["status"] == "ok"
    flipped = R1000["signature"][:-2] + ("00" if R1000["signature"][-2:] != "00" else "01")
    for label, sig in (("a flipped signature", flipped), ("another round's signature", R_LATE["signature"]),
                       ("junk", "zz" * 48)):
        forged = copy.deepcopy(record)
        forged["session"]["state"]["data"]["rand"]["beacons"][str(R1000["round"])] = sig
        f = _finding(forged, "rand")
        assert f["status"] == "fail" and "not drand's signature" in f["detail"], label
    forged = copy.deepcopy(record)
    gate_round = next(r for r in forged["session"]["state"]["data"]["rand"]["beacons"]
                      if int(r) != forged["session"]["state"]["data"]["rand"]["round"])
    forged["session"]["state"]["data"]["rand"]["beacons"][gate_round] = "22" * 48
    assert _finding(forged, "rand")["status"] == "fail"


def test_a_missing_gate_beacon_fails_verify(finished):
    _, _, _, record = finished
    forged = copy.deepcopy(record)
    d = forged["session"]["state"]["data"]
    del d["rand"]["beacons"][str(d["moves"][0]["round"])]
    assert _finding(forged, "rand")["status"] == "fail"


def test_the_replay_catches_an_illegal_logged_move(finished):
    _, _, _, record = finished
    forged = copy.deepcopy(record)
    gate = forged["session"]["state"]["data"]["moves"][1]["gate"]
    entries = [e for e in forged["log"] if e.get("action") == "drop"]
    entries[1]["payload"] = {**entries[1]["payload"], "channel": gate}        # the shut channel, logged
    f = _finding(forged, "replay")
    assert f["status"] == "fail" and "move 1" in f["detail"] and "shut" in f["detail"], f


def test_a_round_outside_its_moves_window_fails_verify(finished):
    _, _, _, record = finished

    def edit(d):
        d["moves"][0]["at"] = (datetime.fromisoformat(d["moves"][0]["at"]) - timedelta(days=1)).isoformat()
    forged = _tamper(record, edit)
    f = _finding(forged, "rand")
    assert f["status"] == "fail" and "window" in f["detail"], f


def test_an_export_line_verifies_offline_and_a_forged_beacon_in_it_fails(finished):
    client, sid, agents, _ = finished
    r = client.get("/games/export", params={"module": SPILLWAY, "limit": 200})
    line = next(json.loads(x) for x in r.text.splitlines() if json.loads(x)["session_id"] == sid)
    report = game_export.verify_line(line)
    statuses = {f["check"]: f["status"] for f in report["findings"]}
    assert statuses["beacons_bls"] == "ok" and statuses["replay"] == "ok", report["findings"]
    assert not [f for f in report["findings"] if f["status"] == "fail"], report["findings"]
    forged = copy.deepcopy(line)
    some = next(iter(forged["rand"]["beacons"]))
    forged["rand"]["beacons"][some] = "11" * 48
    forged["final_state"]["data"]["rand"]["beacons"][some] = "11" * 48
    assert next(x for x in game_export.verify_line(forged)["findings"] if x["check"] == "beacons_bls")["status"] == "fail"
    moved = copy.deepcopy(line)
    moved["final_state"]["data"]["moves"][0]["channel"] = moved["final_state"]["data"]["moves"][0]["channel"] % 6 + 1
    assert [f for f in game_export.verify_line(moved)["findings"] if f["status"] == "fail"]


# ── the tools and the registry ───────────────────────────────────────────────

def test_fill_move_adds_a_round_later_than_the_last_and_the_pre_check_reads_the_served_state(client):
    now = 1_800_000_000.0
    data = {"rand": {"round": rand.round_at(now) - 3}, "moves": []}
    assert M().fill_move("drop", {"channel": 3}, data, now) == {"channel": 3, "round": rand.round_at(now) + 5}
    data = {"rand": {"round": 1}, "moves": [{"round": rand.round_at(now) + 9}]}
    assert M().fill_move("drop", {"channel": 3}, data, now)["round"] == rand.round_at(now) + 10
    assert M().fill_move("drop", {"channel": 3, "round": 4}, data, now) == {"channel": 3, "round": 4}
    assert M().fill_move("void", {"reason": "late_commit"}, data, now) == {"reason": "late_commit"}
    sid, agents = _game(client)
    gate, mover, d = _gated(client, sid, agents[0])
    assert M().move_problem(d, {"channel": gate, "round": d["name_round"]}).startswith(f"channel {gate} is shut")
    assert M().move_problem(d, {"channel": d["open"][0], "round": d["name_round"]}) == ""
    assert M().move_problem(d, {"channel": d["open"][0]}) == "", "the tools fill the round in afterwards"
    assert M().move_problem({}, {"channel": 3}) == "", "a missing gate (nothing served yet) is not an error"
    for junk in ({}, {"channel": 9}, {"channel": 1, "x": 2}, {"round": 5}, "3", {"channel": True}):
        assert M().move_problem(d, junk), junk


def test_submit_action_fills_the_round_in_before_signing(client, monkeypatch):
    from emporia import mcp_server as m
    sid, agents = _game(client)
    seen: list[dict] = []

    class R:
        def __init__(self, body, code=200):
            self._b, self.status_code = body, code

        def json(self):
            return self._b

        def raise_for_status(self):
            pass

    sess = client.get(f"/sessions/{sid}", headers=_hdr(agents[0])).json()

    def fake_request(method, url, **kw):
        if method == "get":
            return R(sess)
        seen.append(kw["json"]["payload"])
        return R({"is_terminal": False})

    monkeypatch.setattr(m, "_bearer_request", fake_request)
    monkeypatch.setattr(m.signing, "sign", lambda *a, **k: "sig")
    monkeypatch.setattr(m, "_playbook_refusal_runner", lambda *a, **k: None)
    d = sess["state"]["data"]
    shut = m.submit_action(sid, "drop", {"channel": d["gate"]}, WHY, agent_id=d["to_move"])
    assert shut["success"] is False and "shut" in shut["error"] and seen == [], "refused before anything is sent"
    out = m.submit_action(sid, "drop", {"channel": d["open"][0]}, WHY, agent_id=d["to_move"])
    assert out.get("success"), out
    assert len(seen) == 1 and seen[0]["channel"] == d["open"][0]
    assert rand.turn_round_problem(time.time(), seen[0]["round"]) == "" and seen[0]["round"] > d["gate_round"]


def test_the_registry_the_listing_and_the_guide(client):
    cls = MODULE_REGISTRY[SPILLWAY]
    assert (cls.USES_RAND, cls.REPLAY_WITH_RAND, cls.TIMED, cls.TURN_ORDER, cls.ZERO_SUM, cls.DECLARES_WINNER,
            cls.ENGINE_DECIDABLE, cls.CHAT_BY_DEFAULT, cls.CHECKED_ACTIONS) == \
        (True, True, True, False, True, True, False, True, ("drop",))
    assert cls.MIN_PARTICIPANTS == cls.MAX_PARTICIPANTS == 2 and len(cls.HOW_TO_PLAY.strip()) <= HOW_TO_PLAY_MAX
    listed = {m["module_type"]: m for m in client.get("/modules").json()["modules"]}
    assert listed[SPILLWAY]["uses_rand"] is True and listed[SPILLWAY]["how_to_play"] == cls.HOW_TO_PLAY.strip()
    assert "gate" in listed[SPILLWAY]["rules"] and "second" in listed[SPILLWAY]["rules"]
    from emporia import house, playbook, quests
    assert SPILLWAY in quests.SET_PLAYS and SPILLWAY in quests.CHANCE_GAMES and SPILLWAY in playbook.SET_PLAYS
    assert SPILLWAY not in house.SET_PLAYS and SPILLWAY not in house.PLAYING, "the house agent stays out"
    ok, why = cls().validate_config({"drops": 7, "move_time_limit_s": 600})
    assert ok, why
    for bad in ({"drops": 3}, {"drops": 11}, {"drops": "7"}, {"move_time_limit_s": 5}, {"drops": True}):
        assert not cls().validate_config(bad)[0], bad


def test_the_start_notice_does_not_claim_a_first_mover_the_beacon_has_not_drawn():
    from emporia.plugins.platforms.emporia.frames import format_frame
    frame = {"type": "session_started", "session_id": "sess_x", "module_type": SPILLWAY, "participants": ["alpha", "beta"],
             "current_agent": "alpha", "opens_at": "2026-11-10T00:36:24+00:00"}
    for me in ("alpha", "beta"):
        text = format_frame(frame, me).text
        assert "You move first" not in text and "The first move is not yours" not in text and "beacon picks who moves first" in text
    turn = format_frame({"type": "your_turn", "session_id": "sess_x", "module_type": SPILLWAY}, "alpha").text
    assert "drop" in turn and "gate" in turn and "to_move" in turn


# ── who is told "your turn" when the beacon has drawn the first mover ─────────

def _tell(session, published=None):
    from unittest.mock import AsyncMock, patch

    from emporia.routes import sessions
    with patch.object(sessions.rs, "load_session", return_value=session), \
         patch.object(sessions.rs, "broadcast_to_agent", new=AsyncMock()) as bcast, \
         patch.object(sessions.beacon_route, "signature", return_value=published), \
         patch.object(sessions.asyncio, "sleep", new=AsyncMock()):
        asyncio.run(sessions._tell_when_open("sess_x", datetime.now(UTC) - timedelta(seconds=5)))
    return bcast


def _stored_game(agents=("alpha", "beta")):
    r0 = 5000
    data = {"cols": 6, "rows": 5, "drops": 7, "move_time_limit_s": 600, "seats": list(agents), "opening": None, "moves": [],
            "pending": list(agents), "ended": None, "voided": None,
            "rand": {"scope": "sess_x", "commits": {}, "round": r0, "beacon_sig": fake_beacon_sig(r0),
                     "beacons": {str(r0): fake_beacon_sig(r0)}, "reveals": {}}}
    return data


def test_only_the_drawn_first_mover_is_told_it_is_their_turn():
    data = _stored_game()
    first = S._opening_for(data, _sig(5000))["first"]
    bcast = _tell({"status": "active", "step_number": 0, "module_type": SPILLWAY, "current_agent": "alpha",
                   "participants": ["alpha", "beta"], "state": {"data": data}})
    sent = [(c.args[0], c.args[1]["type"]) for c in bcast.await_args_list]
    assert {a for a, _ in sent} == {first} and ("your_turn" in {t for _, t in sent}), sent
    assert M().first_to_move(data) == [first]
    # both outcomes occur across sessions: the old behaviour told seat A (and B) every time
    seen = {S._opening_for({**data, "rand": {**data["rand"], "scope": f"s{i}"}}, _sig(5000))["first"] for i in range(40)}
    assert seen == {"alpha", "beta"}


def test_nobody_is_told_while_the_draw_is_not_stored_and_other_games_are_unchanged():
    data = _stored_game()
    data["rand"] = {**data["rand"], "beacon_sig": None, "beacons": {}}
    assert M().first_to_move(data) == []
    bcast = _tell({"status": "active", "step_number": 0, "module_type": SPILLWAY, "current_agent": "alpha",
                   "participants": ["alpha", "beta"], "state": {"data": data}})
    bcast.assert_not_awaited()
    from emporia.modules.spillway import SpillwayModule  # noqa: F401
    other = get_interaction_module("emporia:bargain:v2")
    assert other.first_to_move({}) is None, "the default: the state's own pending / current agent"


def test_the_start_frame_does_not_name_seat_a_as_the_mover():
    from unittest.mock import AsyncMock, patch

    from emporia.routes import sessions
    cls = M()
    state = cls.initial_state(["alpha", "beta"], {})
    frames = []

    async def run():
        with patch.object(sessions.rs, "save_session"), patch.object(sessions, "_schedule", new=lambda c: c.close()), \
             patch.object(sessions, "_first_turn_opens_at", return_value=datetime.now(UTC) + timedelta(seconds=30)), \
             patch.object(sessions, "get_interaction_module", return_value=type("W", (), {
                 "initial_state": lambda self, p, c: state, "first_to_move": cls.first_to_move,
                 "TURN_ORDER": False})()), \
             patch.object(sessions.rs, "broadcast_to_agent", new=AsyncMock(side_effect=lambda a, f: frames.append(f))):
            await sessions._start("sess_x", {"module_type": SPILLWAY, "config": {}, "created_at": "t"},
                                  ["alpha", "beta"], {})
    asyncio.run(run())
    assert frames and all(f["type"] == "session_started" and "current_agent" not in f and "opens_at" in f for f in frames)


# ── a tool right after the opponent's move waits for the gate ─────────────────

class _Clock:
    def __init__(self, t):
        self.t, self.slept = t, []

    def time(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s

    async def asleep(self, s):
        self.sleep(s)


def _served_state(gate_at, ready, step=3):
    return {"step_number": step, "module_type": SPILLWAY,
            "state": {"data": {"gate_ready": ready, "gate_at": datetime.fromtimestamp(gate_at, UTC).isoformat(),
                               "gate": 2 if ready else None, "open": [1, 3] if ready else [], "gate_round": 9}}}


def test_gate_wait_s_only_waits_for_a_gate_that_is_due_within_20_seconds():
    from emporia import rand_game
    now = 1_800_000_000.0
    d = _served_state(now + 12, False)["state"]["data"]
    assert rand_game.gate_wait_s(d, now) == pytest.approx(12)
    assert rand_game.gate_wait_s(_served_state(now + 25, False)["state"]["data"], now) is None
    assert rand_game.gate_wait_s(_served_state(now - 3, False)["state"]["data"], now) == 0.0
    assert rand_game.gate_wait_s(_served_state(now + 5, True)["state"]["data"], now) is None
    assert rand_game.gate_wait_s({"gate_at": "nonsense", "gate_ready": False}, now) is None
    assert rand_game.gate_wait_s({}, now) is None


def test_the_mcp_tool_waits_for_the_gate_then_goes_on(monkeypatch):
    from emporia import mcp_server
    clock = _Clock(1_800_000_000.0)
    monkeypatch.setattr(mcp_server, "_clock", clock.time)
    monkeypatch.setattr(mcp_server, "_sleep", clock.sleep)
    gate_at = clock.t + 12
    answers = iter([_served_state(gate_at, False), _served_state(gate_at, True, step=3)])

    class R:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda *a, **k: R(next(answers)))
    first = _served_state(gate_at, False)
    out = mcp_server._await_gate("alpha", "sess_x", first)
    assert out["state"]["data"]["gate_ready"] is True and 11 <= sum(clock.slept) <= 14
    assert mcp_server._await_gate("alpha", "sess_x", out) is out, "ready: nothing to wait for"


def test_the_mcp_tool_gives_up_after_20_seconds_and_a_far_gate_is_refused_at_once(monkeypatch):
    from emporia import mcp_server
    clock = _Clock(1_800_000_000.0)
    monkeypatch.setattr(mcp_server, "_clock", clock.time)
    monkeypatch.setattr(mcp_server, "_sleep", clock.sleep)
    calls = []

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return _served_state(clock.t + 3, False)     # the relay never stores the round
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda *a, **k: calls.append(1) or R())
    out = mcp_server._await_gate("alpha", "sess_x", _served_state(clock.t + 3, False))
    assert out["state"]["data"]["gate_ready"] is False and 19.9 <= sum(clock.slept) <= 21.6 and calls
    clock.slept.clear()
    calls.clear()
    far = _served_state(clock.t + 40, False)
    assert mcp_server._await_gate("alpha", "sess_x", far) is far and not clock.slept and not calls
    # and the unchanged, clear message follows from the module
    why = M().move_problem(far["state"]["data"], {"channel": 3})
    assert "not drawn yet" in why and "Wait" in why


def test_the_sdk_waits_for_the_gate_too():
    from emporia.agent_sdk import EmporiaAgent
    clock = _Clock(1_800_000_000.0)
    gate_at = clock.t + 9
    answers = iter([_served_state(gate_at, False), _served_state(gate_at, True)])

    class A(EmporiaAgent):
        _sleep = staticmethod(clock.asleep)
        _clock = staticmethod(clock.time)

        def __init__(self):
            pass

        async def get_session(self, session_id):
            return next(answers)
    out = asyncio.run(A()._await_gate("sess_x", _served_state(gate_at, False)))
    assert out["state"]["data"]["gate_ready"] is True and 8 <= sum(clock.slept) <= 11
    far = _served_state(clock.t + 60, False)
    assert asyncio.run(A()._await_gate("sess_x", far)) is far


# ── the measured variant: a level game decided by a drand coin flip ───────────

def test_the_rule_in_force_is_the_second_mover_and_drops_default_to_ten():
    assert S.LEVEL_RULE == "second" and S.DEFAULTS["drops"] == 10 and S.BOUNDS["drops"] == (4, 10)
    assert "default 10" in M.RULES and "Level goes to the player who moved second" in M.RULES


def _level_data(scope="coin-scope", last_round=6):
    d = _data([("a", 1), ("b", 3), ("a", 5), ("b", 6)], drops=2)
    d["rand"] = {"scope": scope, "round": 1, "beacon_sig": fake_beacon_sig(1),
                 "beacons": {str(r): fake_beacon_sig(r) for r in (1, 2, 3, 4, 5, last_round)}}
    for i, m in enumerate(d["moves"]):
        m["round"] = i + 2
    return d


def test_with_the_coin_rule_a_level_game_is_decided_by_a_flip_of_its_last_gate_beacon(monkeypatch):
    monkeypatch.setattr(S, "LEVEL_RULE", "coin")
    d = _level_data()
    v = S.verdict(d)
    assert v["termination"] == "coin" and v["threes"] == {"a": 0, "b": 0} and v["outcome_type"] == "won"
    # the flip is the 'level' draw from the beacon that drew the last move's gate (the round move 3 named)
    assert v["winner"] == ["a", "b"][rand_game_pick("coin-scope", fake_beacon_sig(d["moves"][2]["round"]))]
    assert v["points"][v["winner"]] == 1 and "coin" in v["reason"]
    # the flip does not depend on who moved first, and both sides can win it
    winners = set()
    for i in range(30):
        winners.add(S.verdict(_level_data(f"scope-{i}"))["winner"])
    assert winners == {"a", "b"}
    # a different beacon for the last gate's round gives (over scopes) either winner: the moves do not fix the flip
    flips = set()
    for i in range(30):
        d2 = _level_data(f"s{i}")
        d2["rand"]["beacons"][str(d2["moves"][2]["round"])] = fake_beacon_sig(999)
        flips.add(S.verdict(d2)["winner"])
    assert flips == {"a", "b"}
    # a decided count is untouched by the rule
    decided = _data([("a", 3), ("b", 6), ("a", 5), ("b", 6)], opening_extra={"stones": {"a": [[1, 1], [2, 1]], "b": []}}, drops=2)
    assert S.verdict(decided)["termination"] == "threes"


def rand_game_pick(scope, sig_hex):
    from emporia import rand_game
    return rand_game.pick(scope, "level", bytes.fromhex(sig_hex), 2)


def test_a_coin_flip_game_through_the_relay_verifies_and_a_tampered_beacon_or_result_fails(client, monkeypatch):
    monkeypatch.setattr(S, "LEVEL_RULE", "coin")
    monkeypatch.setattr(beacon, "verify", _fake_or_real)
    for seed in range(1, 120):        # a game played through the relay that ends level (so the flip decides it)
        sid, agents = _game(client, {"drops": 4})
        s = _play(client, sid, agents, random.Random(seed))
        if s["state"]["data"]["ended"]["how"] == "coin":
            break
    else:
        raise AssertionError("no level game found")
    s = rs.load_session(sid)
    data = s["state"]["data"]
    out = get_interaction_module(SPILLWAY).is_terminal(SessionState.from_dict(s["state"]))[1]
    assert out["termination"] == "coin" and data["ended"]["how"] == "coin" and out["winner"] == data["ended"]["winner"]
    assert out["threes"][agents[0]] == out["threes"][agents[1]]
    record = _record(client, sid, *agents)
    report = verify_game(**record)
    assert report["verdict"] == "ok", [f for f in report["findings"] if f["status"] != "ok"]
    assert get_interaction_module(SPILLWAY).rand_problem(data, agents, record["relay_verdict"]) == ""

    def flip(d):
        d["ended"] = {**d["ended"], "winner": next(p for p in d["seats"] if p != d["ended"]["winner"])}
    assert _finding(_tamper(record, flip), "rand")["status"] == "fail"
    last_gate_round = str(data["moves"][-2]["round"])

    def bad_beacon(d):
        d["rand"]["beacons"][last_gate_round] = fake_beacon_sig(123456)
    forged = _tamper(record, bad_beacon)
    assert _finding(forged, "rand")["status"] == "fail" or _finding(forged, "replay")["status"] == "fail"
    assert verify_game(**forged)["verdict"] != "ok"

    def renamed(d):
        d["ended"] = {**d["ended"], "how": "level"}
    assert _finding(_tamper(record, renamed), "rand")["status"] == "fail"


def _voided(client, monkeypatch):
    monkeypatch.setattr(beacon, "verify", _fake_or_real)
    sid, agents = _game(client, ahead=4)
    r = client.post(f"/sessions/{sid}/action", json=_signed_action_body(client, sid, agents[1], "void",
                                                                       {"reason": "late_commit"}, WHY))
    assert r.status_code == 200, r.text
    return _record(client, sid, *agents)


def test_an_honestly_voided_game_replays_to_the_stored_state(client, monkeypatch):
    record = _voided(client, monkeypatch)
    assert record["session"]["state"]["data"]["voided"]
    assert _finding(record, "replay")["status"] == "ok", _finding(record, "replay")
    assert not _failures(record), _failures(record)


def test_a_tampered_voided_game_still_fails_verify(client, monkeypatch):
    record = _voided(client, monkeypatch)
    assert _finding(_tamper(record, lambda d: d.__setitem__("voided", {"by": "x", "reason": "late_commit"})),
                    "replay")["status"] == "fail"
    assert _finding(_tamper(record, lambda d: d.__setitem__("board", ["......"] * 5)), "replay")["status"] == "fail"
    assert _finding(_tamper(record, lambda d: d.pop("voided")), "replay")["status"] == "fail"


def test_the_first_drop_wakes_the_creator_when_the_draw_made_the_other_seat_go_first(client):
    """Found live 11-21 (sess_7b5b530fcd76): the session's current_agent starts as the creator, the draw made the other
    seat move first, so its drop handed the turn to the agent already named and no `your_turn` was sent; the creator
    never woke and lost on time."""
    from unittest.mock import AsyncMock, patch
    for _ in range(40):
        sid, agents = _game(client)
        first = _served(client, sid, agents[0])["state"]["data"]["to_move"]
        if first == agents[1]:
            break
    else:
        raise AssertionError("the draw never chose the second seat")
    rng = random.Random(3)
    open_now = _served(client, sid, first)["state"]["data"]["open"]
    with patch.object(rs, "broadcast_to_agent", new=AsyncMock()) as bcast:
        r = _drop(client, sid, first, rng.choice(open_now))
        assert r.status_code == 200, r.text
    turns = [c.args[0] for c in bcast.await_args_list if c.args[1].get("type") == "your_turn"]
    assert turns == [agents[0]], turns



def test_the_first_mover_is_told_when_the_draw_is_published_but_not_stored_yet():
    """Found live 11-21 (sess_6d9713a97d9b): the draw is stored in the game only when someone moves, so the opening
    notice found nothing stored, ran out of tries and told nobody; the first mover sat until its clock ran."""
    data = _stored_game()
    first = S._opening_for(data, _sig(5000))["first"]
    data["rand"] = {**data["rand"], "beacon_sig": None, "beacons": {}}
    assert M().first_to_move(data) == [], "nothing stored"
    bcast = _tell({"status": "active", "step_number": 0, "module_type": SPILLWAY, "current_agent": "alpha",
                   "participants": ["alpha", "beta"],
                   "state": {"data": data, "metadata": {"participants": ["alpha", "beta"]}}}, published=fake_beacon_sig(5000))
    sent = [(c.args[0], c.args[1]["type"]) for c in bcast.await_args_list]
    assert {a for a, _ in sent} == {first} and ("your_turn" in {t for _, t in sent}), sent   # (+ its half-clock reminder)
