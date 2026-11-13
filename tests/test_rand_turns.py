"""A draw per move (`emporia:rand:v1`, WP G1): a module names more than one drand round, each pinned by a
signed move payload; the relay stores every round's signature under `rand.beacons`; verify checks them all
and replays the signed log. A test-only module (`test:turns:v1`) stands in for a real game; drand is a
fixture throughout (conftest `rand_testmode`, and the two recorded real rounds of test_rand.py for BLS)."""
import asyncio
import copy
import json
import time
import uuid
from typing import Any

import pytest
from conftest import fake_beacon_sig
from fastapi.testclient import TestClient

from emporia import beacon, game_export, rand, rand_game
from emporia import relay_server as rs
from emporia.module_sdk import (
    MODULE_REGISTRY,
    InteractionModule,
    SessionResult,
    SessionState,
    list_available_modules,
    register_module,
)
from emporia.relay_server import app
from emporia.routes import beacon as beacon_route
from emporia.routes.sessions import _with_beacon
from emporia.verify import verify_game
from tests.test_emporia import _register_keyed, _signed_action_body
from tests.test_rand import R1000, R_LATE

TURNS = "test:turns:v1"
WHY = "Reading the gate and the board"
MOVES = 4


class TurnsGame(InteractionModule):
    """Two players alternate four steps. Before step n a gate (1-3) is drawn from the round named in step n-1
    (the game's own round for step 1); a step may not pick the gate. Most points wins."""
    MODULE_TYPE = TURNS
    DECLARES_WINNER = True
    USES_RAND = True
    REPLAY_WITH_RAND = True
    CHECKED_ACTIONS = ("step",)
    RULES = "Pick 1-3 and name the next drand round; you may not pick the gate."
    MIN_PARTICIPANTS = MAX_PARTICIPANTS = 2

    def initial_state(self, participants, config):
        r = dict(config.get("rand") or {})
        commits = dict(r.get("commits") or {})
        rnd = rand_game.game_round(commits, 2)
        return SessionState(
            data={"seats": list(participants), "moves": [], "ended": None, "turn_started_at": None,
                  "rand": {"scope": r.get("scope", ""), "commits": commits, "round": rnd, "beacon_sig": None,
                           "beacons": {}, "reveals": {}}},
            current_agent=participants[0], step_number=0, metadata={"participants": participants})

    def _needed(self, data):
        moves = data["moves"]
        return data["rand"]["round"] if not moves else moves[-1]["round"]

    def beacon_round(self, state):
        d = state.data
        if len(state.metadata.get("participants", [])) < 2 or d.get("ended") or not d["rand"].get("round"):
            return None
        need = self._needed(d)
        return need if need and str(need) not in d["rand"]["beacons"] else None

    def with_beacon(self, state, round_no, sig):
        r = {**state.data["rand"], "beacons": {**state.data["rand"]["beacons"], str(round_no): sig}}
        if round_no == r["round"]:
            r["beacon_sig"] = sig
        return SessionState(data={**state.data, "rand": r}, current_agent=state.current_agent,
                            step_number=state.step_number, metadata=state.metadata)

    def gate(self, data) -> int | None:
        sig = rand_game.beacon_of(data, self._needed(data))
        return rand_game.pick(data["rand"]["scope"], f"gate:{len(data['moves']) + 1}", sig, 3) + 1 if sig else None

    def fill_move(self, action_type, payload, data, now_ts):
        return {**payload, "round": rand.client_turn_round(now_ts)} if action_type == "step" and "round" not in payload \
            else payload

    def move_problem(self, data, move):
        if not isinstance(move, dict) or set(move) != {"pick", "round"}:
            return 'a step is {"pick": 1-3, "round": the drand round of the next gate}'
        if isinstance(move["pick"], bool) or move["pick"] not in (1, 2, 3):
            return "pick: a whole number 1-3"
        if (g := self.gate(data)) is not None and move["pick"] == g:
            return f"channel {move['pick']} is shut this turn"
        return rand.turn_round_problem(rand_game.now().timestamp(), move["round"])

    def validate_action(self, state, action):
        if action.action_type != "step":
            return False, "send 'step'"
        if state.data.get("ended"):
            return False, "the game is over"
        if self.gate(state.data) is None:
            return False, "the gate is not drawn yet: its drand round is not published"
        why = self.move_problem(state.data, action.payload)
        return (False, why) if why else (True, "")

    def apply_action(self, state, action):
        if self.gate(state.data) == action.payload["pick"]:       # as a real rule does: a replay has no validate
            raise ValueError(f"channel {action.payload['pick']} is shut")
        d = {**state.data, "moves": list(state.data["moves"])}
        d["moves"].append({"n": len(d["moves"]) + 1, "by": action.agent_id, "pick": action.payload["pick"],
                           "gate": self.gate(state.data), "round": action.payload["round"],
                           "at": rand_game.now().isoformat()})
        if len(d["moves"]) == MOVES:
            d["ended"] = True
        seats = state.metadata["participants"]
        new = SessionState(data=d, current_agent=seats[len(d["moves"]) % 2], step_number=state.step_number + 1,
                           metadata=state.metadata)
        over, result = self.is_terminal(new)
        return SessionResult(success=True, new_state=new, artifacts={"result": result} if over else None)

    def is_terminal(self, state):
        d = state.data
        if not d.get("ended"):
            return False, {}
        pts = {p: sum(m["pick"] for m in d["moves"] if m["by"] == p) for p in d["seats"]}
        a, b = d["seats"]
        win = a if pts[a] > pts[b] else b if pts[b] > pts[a] else None
        return True, {"winner": win, "outcome_type": "won" if win else "draw", "termination": "moves",
                      "points": pts}


@pytest.fixture(autouse=True)
def _turns_module():
    """Registered for these tests only: the dashboard's game list is pinned to the real registry."""
    register_module(TurnsGame)
    yield
    MODULE_REGISTRY.pop(TURNS, None)


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51240))


def _hdr(agent):
    return {"x-emporia-agent-id": agent}


def _game(client):
    agents = [f"rt{i}_{uuid.uuid4().hex[:6]}" for i in range(2)]
    for a in agents:
        _register_keyed(client, a)
    r = client.post("/sessions", json={"module_type": TURNS, "config": {}}, headers=_hdr(agents[0]))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    r = client.post(f"/sessions/{sid}/join", json={"agent_id": agents[1]}, headers=_hdr(agents[1]))
    assert r.status_code == 200, r.text
    return sid, agents


def _gate_now(sid):
    s = rs.load_session(sid)
    data = s["state"]["data"]
    need = data["moves"][-1]["round"] if data["moves"] else data["rand"]["round"]
    sig = bytes.fromhex(fake_beacon_sig(need))        # what the relay will fetch for it
    return rand_game.pick(data["rand"]["scope"], f"gate:{len(data['moves']) + 1}", sig, 3) + 1


def _step(client, sid, who, pick=None, rnd="fill"):
    s = rs.load_session(sid)
    if pick is None:
        gate = _gate_now(sid)
        pick = next(p for p in (1, 2, 3) if p != gate)
    payload: dict[str, Any] = {"pick": pick}
    if rnd == "fill":
        payload = TurnsGame().fill_move("step", payload, s["state"]["data"], time.time())
        payload["round"] += len(s["state"]["data"]["moves"])         # moves in one second still name later rounds
    elif rnd is not None:
        payload["round"] = rnd
    return client.post(f"/sessions/{sid}/action", json=_signed_action_body(client, sid, who, "step", payload, WHY))


def _play(client, sid, agents):
    for i in range(MOVES):
        r = _step(client, sid, agents[i % 2])
        assert r.status_code == 200, r.text


# ── the round rule ───────────────────────────────────────────────────────────

def test_a_turn_round_is_three_to_forty_rounds_ahead_and_the_error_says_what_to_send(monkeypatch):
    monkeypatch.setattr(rand, "MIN_TURN_LEAD_ROUNDS", 3)
    assert (rand.MIN_TURN_LEAD_ROUNDS, rand.CLIENT_TURN_LEAD_ROUNDS, rand.MAX_TURN_LEAD_ROUNDS) == (3, 5, 40)
    now = rand.GENESIS + 3000 * rand.PERIOD_S + 1
    cur = rand.round_at(now)
    assert rand.client_turn_round(now) == cur + 5
    assert rand.turn_round_problem(now, cur + 3) == "" and rand.turn_round_problem(now, cur + 40) == ""
    assert rand.turn_round_problem(now, rand.client_turn_round(now)) == ""
    early, late = rand.turn_round_problem(now, cur + 2), rand.turn_round_problem(now, cur + 41)
    assert "too early" in early and f"Send round {cur + 5}" in early
    assert "too far ahead" in late and f"Send round {cur + 5}" in late
    for junk in (True, "5", None, 0, -3, 2.5):
        assert "whole number" in rand.turn_round_problem(now, junk) and f"{cur + 5}" in rand.turn_round_problem(now, junk)


def test_a_stored_beacon_is_read_back_and_a_draw_is_deterministic():
    data = {"rand": {"scope": "s", "beacons": {"7": fake_beacon_sig(7)}}}
    assert rand_game.beacon_of(data, 7) == bytes.fromhex(fake_beacon_sig(7))
    assert rand_game.beacon_of(data, 8) == b"" and rand_game.beacon_of({}, 7) == b""
    sig = bytes.fromhex(fake_beacon_sig(7))
    v = rand_game.pick("s", "gate:1", sig, 6)
    assert v == rand.uint(rand.rand("s", "gate:1", sig)) % 6 == rand_game.pick("s", "gate:1", sig, 6)
    assert {rand_game.pick("s", f"p:{i}", sig, 4) for i in range(60)} == {0, 1, 2, 3}


# ── the SDK hooks ────────────────────────────────────────────────────────────

def test_the_defaults_leave_every_existing_game_as_it_was():
    cls = MODULE_REGISTRY["emporia:liars-dice:v1"]
    assert cls.REPLAY_WITH_RAND is False and cls().replay_state({"state": {"data": {"rand": {}}}}) is None
    payload = {"quantity": 1, "face": 2}
    assert cls().fill_move("bid", payload, {}, 1.0) is payload
    st = SessionState(data={"rand": {"round": 5, "beacon_sig": None}}, current_agent="a", step_number=3,
                      metadata={"participants": ["a", "b"]})
    out = cls().with_beacon(st, 5, "ab" * 48)
    assert out.data["rand"] == {"round": 5, "beacon_sig": "ab" * 48} and "beacons" not in out.data["rand"]
    assert (out.current_agent, out.step_number, out.metadata) == ("a", 3, st.metadata)


def test_fill_move_adds_the_round_and_the_state_is_what_replay_starts_from():
    g, now = TurnsGame(), 1_800_000_000.0
    assert g.fill_move("step", {"pick": 2}, {}, now) == {"pick": 2, "round": rand.round_at(now) + 5}
    assert g.fill_move("step", {"pick": 2, "round": 9}, {}, now) == {"pick": 2, "round": 9}
    final = {"rand": {"round": 11, "beacon_sig": "aa" * 48, "beacons": {"11": "aa" * 48, "20": "bb" * 48}}}
    st = g.replay_state({"participants": ["a", "b"], "config": {"rand": {"scope": "s", "commits": {}}},
                         "state": {"data": final}})
    assert st.data["moves"] == [] and st.data["rand"]["beacons"] == final["rand"]["beacons"]
    assert st.data["rand"]["round"] == 11 and st.data["rand"]["scope"] == "s"
    assert g.replay_state({"participants": ["a", "b"], "state": {"data": {}}}) is None


def test_modules_lists_uses_rand(client):
    by = {m["module_type"]: m for m in client.get("/modules").json()["modules"]}
    assert by[TURNS]["uses_rand"] is True and by["emporia:liars-dice:v1"]["uses_rand"] is True
    assert by["emporia:chess:v1"]["uses_rand"] is False and by["emporia:blotto:v2"]["uses_rand"] is False
    assert all(isinstance(m["uses_rand"], bool) for m in list_available_modules())


def test_the_mcp_tools_read_uses_rand_from_the_relay_when_the_module_is_not_installed(monkeypatch):
    from emporia import mcp_server as m

    class R:
        status_code = 200

        def json(self):
            return {"modules": [{"module_type": "acme:far:v1", "uses_rand": True},
                                {"module_type": "acme:near:v1", "uses_rand": False}]}

    asked = []
    monkeypatch.setattr(m, "_bearer_request", lambda *a, **k: asked.append(a[1]) or R())
    assert m._uses_rand("acme:far:v1") is True and m._uses_rand("acme:near:v1") is False
    assert m._uses_rand("acme:unlisted:v1") is False and asked and all(u.endswith("/modules") for u in asked)
    asked.clear()
    assert m._uses_rand("emporia:liars-dice:v1") is True and m._uses_rand("emporia:chess:v1") is False
    assert asked == [], "an installed module is read locally"
    monkeypatch.setattr(m, "_bearer_request", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert m._uses_rand("acme:far:v1") is False


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
    gate = _gate_now(sid)
    pick = next(p for p in (1, 2, 3) if p != gate)
    out = m.submit_action(sid, "step", {"pick": pick}, WHY, agent_id=agents[0])
    assert out.get("success"), out
    assert seen == [{"pick": pick, "round": rand.client_turn_round(time.time())}] or \
        (len(seen) == 1 and seen[0]["pick"] == pick and rand.turn_round_problem(time.time(), seen[0]["round"]) == "")


# ── the relay ────────────────────────────────────────────────────────────────

def test_a_round_too_early_or_too_late_is_refused_with_what_to_send(client, monkeypatch):
    monkeypatch.setattr(rand, "MIN_TURN_LEAD_ROUNDS", 3)
    sid, agents = _game(client)
    cur = rand.round_at(time.time())
    for rnd, word in ((cur, "too early"), (cur + 1, "too early"), (cur + 60, "too far ahead"), ("soon", "whole number")):
        r = _step(client, sid, agents[0], rnd=rnd)
        assert r.status_code == 400 and word in r.text and "Send round" in r.text, r.text
    assert rs.load_session(sid)["state"]["data"]["moves"] == []
    r = _step(client, sid, agents[0], rnd=cur + 5)
    assert r.status_code == 200, r.text


def test_the_gate_round_is_fetched_and_stored_under_beacons(client):
    sid, agents = _game(client)
    assert _step(client, sid, agents[0]).status_code == 200
    d = rs.load_session(sid)["state"]["data"]
    r0, r2 = d["rand"]["round"], d["moves"][0]["round"]
    assert list(d["rand"]["beacons"]) == [str(r0)] and d["rand"]["beacon_sig"] == fake_beacon_sig(r0)
    assert _step(client, sid, agents[1]).status_code == 200
    d = rs.load_session(sid)["state"]["data"]
    assert d["rand"]["beacons"] == {str(r0): fake_beacon_sig(r0), str(r2): fake_beacon_sig(r2)}
    assert d["moves"][1]["gate"] == _gate_now_for(d, 2)


def _gate_now_for(data, n):
    need = data["rand"]["round"] if n == 1 else data["moves"][n - 2]["round"]
    return rand_game.pick(data["rand"]["scope"], f"gate:{n}", bytes.fromhex(fake_beacon_sig(need)), 3) + 1


def test_a_round_not_yet_published_is_not_stored_and_the_move_waits(client, monkeypatch):
    sid, agents = _game(client)
    assert _step(client, sid, agents[0]).status_code == 200
    named = rs.load_session(sid)["state"]["data"]["moves"][0]["round"]
    monkeypatch.setattr(beacon_route, "signature", lambda rnd: None if rnd == named else fake_beacon_sig(rnd))
    r = _step(client, sid, agents[1], pick=1)
    assert r.status_code == 400 and "not published" in r.text, r.text
    assert str(named) not in rs.load_session(sid)["state"]["data"]["rand"]["beacons"]
    monkeypatch.setattr(beacon_route, "signature", fake_beacon_sig)
    assert _step(client, sid, agents[1]).status_code == 200


def test_a_shut_channel_is_refused(client):
    sid, agents = _game(client)
    r = _step(client, sid, agents[0], pick=_gate_now(sid))
    assert r.status_code == 400 and "shut" in r.text


def test_with_beacon_loops_while_a_new_round_is_named_but_at_most_three_times(monkeypatch):
    class Chain(InteractionModule):
        MODULE_TYPE = "test:chain:v1"
        USES_RAND = True

        def beacon_round(self, state):
            have = state.data["rand"].get("beacons", {})
            return len(have) + 1 if len(have) < 9 else None

        def with_beacon(self, state, round_no, sig):
            r = {**state.data["rand"], "beacons": {**state.data["rand"].get("beacons", {}), str(round_no): sig}}
            return SessionState(data={**state.data, "rand": r}, current_agent=None, step_number=0, metadata={})

        initial_state = validate_action = apply_action = is_terminal = lambda *a, **k: None

    asked = []
    monkeypatch.setattr(beacon_route, "signature", lambda rnd: asked.append(rnd) or fake_beacon_sig(rnd))
    st = SessionState(data={"rand": {}}, current_agent=None, step_number=0, metadata={})
    out = asyncio.run(_with_beacon(Chain(), "s", st))
    assert asked == [1, 2, 3] and sorted(out.data["rand"]["beacons"]) == ["1", "2", "3"]

    class Stuck(Chain):      # names a round whose signature it never stores: no endless loop
        def with_beacon(self, state, round_no, sig):
            return state

        def beacon_round(self, state):
            return 7
    asked.clear()
    asyncio.run(_with_beacon(Stuck(), "s", st))
    assert asked == [7]
    monkeypatch.setattr(beacon_route, "signature", lambda rnd: None)      # not published: unchanged
    assert asyncio.run(_with_beacon(Chain(), "s", st)) is st


# ── verify ───────────────────────────────────────────────────────────────────

def _fake_or_real(rnd, sig):
    """The test beacons are fake; the recorded real rounds get the real BLS check."""
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
    sid, agents = _game(client)
    _play(client, sid, agents)
    assert rs.load_session(sid)["status"] == "completed"
    return client, sid, agents, _record(client, sid, *agents)


def _finding(record, check):
    return next(f for f in verify_game(**record)["findings"] if f["check"] == check)


def test_an_honest_game_checks_every_round_and_the_replay(finished):
    _, _, _, record = finished
    assert len(record["session"]["state"]["data"]["rand"]["beacons"]) == MOVES, "the game's round and three gates"
    report = verify_game(**record)
    assert report["verdict"] == "ok", [f for f in report["findings"] if f["status"] != "ok"]
    assert _finding(record, "rand")["status"] == "ok" and _finding(record, "replay")["status"] == "ok"
    assert "4 signed move" in _finding(record, "replay")["detail"]


def test_a_tampered_beacons_entry_fails_verify_with_the_recorded_real_rounds(finished):
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
    a_round = next(r for r in forged["session"]["state"]["data"]["rand"]["beacons"] if int(r) != R1000["round"])
    forged["session"]["state"]["data"]["rand"]["beacons"][a_round] = "22" * 48    # a gate round re-signed
    assert _finding(forged, "rand")["status"] == "fail"


def test_a_beacons_entry_that_is_not_a_round_fails(finished):
    _, _, _, record = finished
    record["session"]["state"]["data"]["rand"]["beacons"]["soon"] = fake_beacon_sig(1)
    assert _finding(record, "rand")["status"] == "fail"


def test_the_replay_catches_an_illegal_logged_move(finished):
    _, _, _, record = finished
    forged = copy.deepcopy(record)
    state_moves = forged["session"]["state"]["data"]["moves"]
    gate = state_moves[1]["gate"]
    entries = [e for e in forged["log"] if e.get("action") == "step"]
    entries[1]["payload"] = {**entries[1]["payload"], "pick": gate}        # the shut channel, logged
    f = _finding(forged, "replay")
    assert f["status"] == "fail" and "move 1" in f["detail"] and "shut" in f["detail"], f


def test_a_board_that_is_not_what_the_moves_give_fails_the_replay(finished):
    _, _, _, record = finished
    forged = copy.deepcopy(record)
    first = forged["session"]["state"]["data"]["moves"][0]
    first["pick"] = first["pick"] % 3 + 1
    f = _finding(forged, "replay")
    assert f["status"] == "fail" and "stored" in f["detail"]
    dropped = copy.deepcopy(record)
    dropped["log"] = [e for e in dropped["log"] if not (e.get("action") == "step" and e.get("step_number") == 3)]
    assert _finding(dropped, "replay")["status"] == "fail"


def test_a_module_that_does_not_replay_gets_no_replay_finding(client, monkeypatch):
    monkeypatch.setattr(beacon, "verify", lambda rnd, sig: sig == fake_beacon_sig(rnd))
    monkeypatch.setattr(TurnsGame, "REPLAY_WITH_RAND", False)
    sid, agents = _game(client)
    _play(client, sid, agents)
    record = _record(client, sid, *agents)
    assert not [f for f in verify_game(**record)["findings"] if f["check"] == "replay"]
    assert _finding(record, "rand")["status"] == "ok"


# ── the open export ──────────────────────────────────────────────────────────

def test_the_export_carries_every_round_and_the_offline_check_tests_them_all(finished):
    client, sid, agents, _ = finished
    r = client.get("/games/export", params={"module": TURNS, "limit": 200})
    line = next(json.loads(x) for x in r.text.splitlines() if json.loads(x)["session_id"] == sid)
    beacons = line["rand"]["beacons"]
    assert len(beacons) == MOVES and set(beacons) == set(line["final_state"]["data"]["rand"]["beacons"])
    report = game_export.verify_line(line)
    statuses = {f["check"]: f["status"] for f in report["findings"]}
    assert statuses["beacons_bls"] == "ok" and statuses["replay"] == "ok", report["findings"]
    forged = copy.deepcopy(line)
    some = next(iter(forged["rand"]["beacons"]))
    forged["rand"]["beacons"][some] = "11" * 48
    forged["final_state"]["data"]["rand"]["beacons"][some] = "11" * 48
    f = next(x for x in game_export.verify_line(forged)["findings"] if x["check"] == "beacons_bls")
    assert f["status"] == "fail" and some in f["detail"]
