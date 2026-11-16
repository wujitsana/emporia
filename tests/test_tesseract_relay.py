"""Tesseract T1 through the relay: tables of scripted agents on a throwaway relay with the test beacon and a fake clock.

Every test builds its own world, its own agents (unique names) and its own tables, and leaves nothing behind in module
state: the clock, the beacon and the module switch are put back when the world closes, so the file passes in any order.
The agents run the real tools' logic (`tesseract_client.Table`, over the relay's routes). Every finished table is also
checked by `scripts/verify_tesseract.py` and by the module's own `rand_problem`; the tamper tests show both fail."""
from __future__ import annotations

import copy
import json
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import e2e_tesseract as E
import verify_tesseract as V

from emporia import commit_reveal, relay_modules
from emporia import relay_server as rs
from emporia.module_sdk import SessionState, get_interaction_module
from emporia.modules import tesseract as T
from tests.test_emporia import _signed_action_body

GAME = T.GAME_ID


@pytest.fixture
def w():
    world = E.World(TestClient(rs.app, client=("127.0.0.1", 51780)))
    yield world
    world.close()


def agents(w: E.World, n: int, **kw) -> list[str]:
    tag = uuid.uuid4().hex[:6]
    return [w.register(f"ts{tag}_{i}", **kw) for i in range(n)]


def get_json(w: E.World):
    return lambda path: w.client.get(path).json()


def verdict_of(w: E.World, sid: str) -> dict:
    s = w.state(sid)
    rows = w.client.get("/results", params={"module_type": GAME, "agent_id": s["participants"][0], "limit": 50}).json()
    got = next((r for r in rows["results"] if r["session_id"] == sid), None)
    if got is None:      # a practice table is not listed in /results: read its verdict as the listing would have
        assert T.is_practice(s.get("config")), "only a practice table is missing from /results"
        from emporia.routes.standing import _result_of
        got = _result_of(s)
    return got


def ledger_of(w: E.World, sid: str) -> list[dict]:
    return [r for r in w.client.get("/tesseract/results", params={"limit": 200}).json()["results"]
            if r["session_id"] == sid]


def finished(w: E.World, n: int = 5, strategies=None, **kw):
    names = agents(w, n)
    sid = w.table(names)
    play = E.play(w, sid, names, strategies or {}, **kw)
    return sid, names, play


def verified(w: E.World, sid: str) -> None:
    rep = V.verify(get_json(w), bls=False, only=sid)
    assert rep["tables"][0]["ok"], rep["tables"][0]["problems"]
    sess = w.state(sid)
    assert get_interaction_module(GAME).rand_problem(sess["state"]["data"], sess["participants"], verdict_of(w, sid)) == ""


def raw_act(w: E.World, sid: str, who: str, action: str, payload: dict):
    body = _signed_action_body(w.client, sid, who, action, payload, "a move made by hand in a test")
    return w.client.post(f"/sessions/{sid}/action", json=body)


# ── a table of five ──────────────────────────────────────────────────────────────────────────────────────

def test_five_players_play_five_rounds_and_everything_verifies(w):
    names = agents(w, 5)
    strategies = dict(zip(names, ["lite", "lite", "random", "idle", "lite"], strict=True))
    sid = w.table(names)
    sess = E.play(w, sid, names, strategies, seed=3, say=True)
    assert sess["status"] == "completed"
    data = sess["state"]["data"]
    assert [k for k in data["published"] if k != "opening"] == ["1", "2", "3", "4", "5"]
    v = verdict_of(w, sid)
    assert v["termination"] == "table" and set(v["points"]) == set(names)
    assert v["winner"] == max(v["points"], key=v["points"].get)
    rows = sorted(ledger_of(w, sid), key=lambda r: r["place"])
    assert [r["place"] for r in rows] == [1, 2, 3, 4, 5]
    assert [r["total"] for r in rows] == sorted((r["total"] for r in rows), reverse=True)
    assert all(r["status"] == "finished" and r["seats"] == 5 for r in rows)
    assert 0 <= sum(r["table_index"] for r in rows) <= 5 * 1000, "an average seat is 1000 at any table size"
    assert rows[0]["won"] == 1 and sum(r["won"] for r in rows) == 1
    # the idle seat scored exactly nothing, the targets are public now, and the chat of the table is on record
    idle = next(n for n in names if strategies[n] == "idle")
    assert v["points"][idle] == 0
    assert set(data["targets"]) == set(names)
    verified(w, sid)


def test_the_epoch_record_the_relay_stores_is_the_one_the_viewer_is_served(w):
    sid, names, sess = finished(w, 4)
    served = w.client.get(f"/sessions/{sid}", headers=w.hdr(names[0])).json()["state"]["data"]
    assert served["board"]["done"] == [served["published"][str(e)] for e in range(1, 6)]
    assert served["board"]["phase"] == "done" or served["board"]["epoch"] == 6
    assert served["board"]["opening"] == served["published"]["opening"]


def test_all_idle_players_draw(w):
    names = agents(w, 3)
    sid = w.table(names)
    sess = E.play(w, sid, names, dict.fromkeys(names, "idle"))
    out = get_interaction_module(GAME).is_terminal(SessionState.from_dict(sess["state"]))[1]
    assert out["outcome_type"] == "draw" and sorted(out["tied"]) == sorted(names) and out["winner"] is None
    assert set(out["points"].values()) == {0}


# ── the paths that go wrong ──────────────────────────────────────────────────────────────────────────────

def test_a_player_who_seals_nothing_in_a_round_pushes_nothing_and_the_game_goes_on(w):
    names = agents(w, 4)
    sid = w.table(names)
    quiet = names[2]
    sess = E.play(w, sid, names, {}, sit_out={(quiet, 3)})
    assert sess["status"] == "completed"
    pub = sess["state"]["data"]["published"]
    assert quiet not in pub["3"]["pushes"] and quiet in pub["3"]["silent"]
    assert any(quiet in pub[str(e)]["pushes"] for e in (1, 2, 4, 5)), "it plays on in the other rounds"
    verdict = verdict_of(w, sid)
    assert not (set(names) - set(verdict["points"]))
    row = next(r for r in ledger_of(w, sid) if r["agent_id"] == quiet)
    assert row["status"] == "finished", "sitting out is not being out"
    verified(w, sid)


def test_a_player_who_seals_and_does_not_open_is_out_with_nothing_and_last_place(w):
    names = agents(w, 4)
    sid = w.table(names)
    bad = names[1]
    sess = E.play(w, sid, names, {}, no_open={(bad, 2)})
    assert sess["status"] == "completed"
    pub = sess["state"]["data"]["published"]
    assert bad in pub["2"]["late"] and bad not in pub["2"]["pushes"], "its move counts as empty for everyone"
    assert bad not in pub["3"]["pushes"] and bad not in pub["4"]["pushes"], "and it is out of the later rounds"
    verdict = verdict_of(w, sid)
    assert verdict["points"][bad] == 0
    rows = sorted(ledger_of(w, sid), key=lambda r: r["place"])
    assert rows[-1]["agent_id"] == bad and rows[-1]["status"] == "out" and rows[-1]["table_index"] == 0
    assert bad in get_interaction_module(GAME).is_terminal(SessionState.from_dict(w.state(sid)["state"]))[1]["out"]
    verified(w, sid)


def test_a_player_who_is_out_is_told_so_and_cannot_seal_again(w):
    names = agents(w, 3)
    sid = w.table(names)
    bad = names[1]
    limit = int(w.state(sid)["state"]["data"]["move_time_limit_s"])
    for e in (1, 2):
        w.clock.advance(20)
        for n in names:
            assert w.tools(n).push(sid, {"XY>X": 2})["success"]
        for n in names:
            if (n, e) != (bad, 2):
                assert w.tools(n).open(sid)["success"]
    w.clock.advance(limit + 25)
    w.sweep()
    v = w.tools(bad).view(sid)
    assert v["out"] is True and v["due"] is None
    refused = w.tools(bad).push(sid, {})
    assert not refused["success"] and "out of this game" in refused["error"]
    assert "out" in w.tools(bad).open(sid)["note"]
    assert "You are out" in raw_act(w, sid, bad, "seal", {"commitment": "0" * 64, "epoch": 3, "round": 10**9}).text


def test_a_player_who_keeps_its_secret_scores_zero_and_is_last(w):
    names = agents(w, 4)
    sid = w.table(names)
    hider = names[0]
    sess = E.play(w, sid, names, {}, no_reveal={hider})
    assert sess["status"] == "completed"
    out = get_interaction_module(GAME).is_terminal(SessionState.from_dict(sess["state"]))[1]
    assert out["unrevealed"] == [hider] and out["points"][hider] == 0 and out["order"][-1] == hider
    assert hider not in sess["state"]["data"]["targets"]
    assert ledger_of(w, sid) and next(r for r in ledger_of(w, sid) if r["agent_id"] == hider)["status"] == "unrevealed"
    verified(w, sid)


def test_a_move_over_budget_is_refused_by_the_tool_and_by_the_relay(w):
    names = agents(w, 3)
    sid = w.table(names)
    w.clock.advance(20)
    t = w.tools(names[0])
    # the tool says no before anything is sent: nine units, a fifth channel, five on a channel, an unknown channel
    for bad, word in (({"XY>X": 4, "XY>Y": 4, "WX>W": 1}, "over budget"),
                      ({"XY>X": 2, "XY>Y": 2, "WX>W": 2, "WX>X": 1, "WY>W": 1}, "too many channels"),
                      ({"XY>X": 5}, "per-channel"), ({"nope": 1}, "unknown channel"), ({"XY>X": 1.5}, "whole")):
        r = t.push(sid, bad)
        assert not r["success"] and word in r["error"] and "Nothing was sent" in r["error"], (bad, r)
    assert (w.state(sid)["state"]["data"].get("seals") or {}) == {}, "nothing was sealed"
    # a hand-built client seals an illegal move (the relay sees a hash) and cannot open it
    mv = {"push": {"XY>X": 4, "XY>Y": 4, "WX>W": 4}}
    nonce = commit_reveal.new_nonce()
    data = w.state(sid)["state"]["data"]
    seal = get_interaction_module(GAME).fill_move("seal", {"commitment": commit_reveal.commitment(mv, nonce),
                                                          "epoch": 1}, data, w.clock.t)
    me = names[1]
    assert raw_act(w, sid, me, "seal", seal).status_code == 200
    for n in (names[0], names[2]):
        assert w.tools(n).push(sid, {}).get("success")
    refused = raw_act(w, sid, me, "open", {"move": mv, "nonce": nonce, "epoch": 1})
    assert refused.status_code == 400 and "not legal" in refused.text and "over budget" in refused.text


def test_a_second_seat_for_one_operator_is_refused_and_another_operator_is_welcome(w):
    a, b = agents(w, 2)
    twin = w.register(f"ts{uuid.uuid4().hex[:6]}_twin", operator=a)
    other = w.register(f"ts{uuid.uuid4().hex[:6]}_other")
    sid = w.create(a, players=3)
    r = w.join(sid, twin, 1)
    assert r.status_code == 409 and "one seat per operator" in r.text
    assert w.join(sid, other, 1).status_code == 200
    assert w.join(sid, b, 2).status_code == 200
    assert w.state(sid)["status"] == "active"
    assert twin not in w.state(sid)["participants"]


def test_the_actions_the_relay_refuses(w):
    names = agents(w, 3)
    sid = w.table(names)
    w.clock.advance(20)
    a, b, c = names
    mod = get_interaction_module(GAME)
    data = w.state(sid)["state"]["data"]
    mv = {"push": {"XY>X": 2}}
    nonce = commit_reveal.new_nonce()
    seal = mod.fill_move("seal", {"commitment": commit_reveal.commitment(mv, nonce), "epoch": 1}, data, w.clock.t)
    # an open before everyone has sealed; a reveal before the rounds are played; the wrong epoch; a bad commitment
    assert "Not now" in raw_act(w, sid, a, "open", {"move": mv, "nonce": nonce, "epoch": 1}).text
    assert "Reveal your secret after round 5" in raw_act(w, sid, a, "reveal", {"s": "0" * 64, "nonce": "0" * 64}).text
    assert "name epoch 1" in raw_act(w, sid, a, "seal", {**seal, "epoch": 2}).text
    assert "sha256" in raw_act(w, sid, a, "seal", {**seal, "commitment": "nope"}).text
    assert "later than" in raw_act(w, sid, a, "seal", {**seal, "round": data["rand"]["round"]}).text
    assert raw_act(w, sid, a, "seal", {**seal, "extra": 1}).status_code == 400
    assert raw_act(w, sid, a, "seal", seal).status_code == 200
    assert "already did your seal" in raw_act(w, sid, a, "seal", seal).text
    outsider = w.register(f"ts{uuid.uuid4().hex[:6]}_out")
    assert raw_act(w, sid, outsider, "seal", seal).status_code in (400, 403)
    for n in (b, c):
        assert w.tools(n).push(sid, {}).get("success")
    # the open must match the seal
    assert "does not match your seal" in raw_act(w, sid, a, "open", {"move": {"push": {"XY>X": 3}}, "nonce": nonce,
                                                                      "epoch": 1}).text
    assert raw_act(w, sid, a, "open", {"move": mv, "nonce": nonce, "epoch": 1}).status_code == 200


def test_the_secret_cannot_be_revealed_with_a_wrong_nonce_or_twice(w):
    sid, names, sess = finished(w, 3, finish=False)
    w.clock.advance(20)
    from conftest import RAND_SECRETS
    s, nonce = RAND_SECRETS[(sid, names[0])]
    assert "not what your commitment hashes" in raw_act(w, sid, names[0], "reveal", {"s": s, "nonce": s}).text
    assert raw_act(w, sid, names[0], "reveal", {"s": s, "nonce": nonce}).status_code == 200
    assert "already revealed" in raw_act(w, sid, names[0], "reveal", {"s": s, "nonce": nonce}).text


# ── what an agent sees ───────────────────────────────────────────────────────────────────────────────────

def test_the_view_is_bounded_private_and_names_the_next_tool(w):
    names = agents(w, 4)
    sid = w.table(names)
    w.clock.advance(20)
    v = w.tools(names[0]).view(sid)
    assert v["due"] == "tesseract_push" and v["phase"] == "seal" and v["ready"] and len(v["channels"]) == 24
    assert set(v["you"]) >= {"target", "gap", "distance", "points", "total", "best"} and len(v["you"]["best"]) == 6
    assert v["limits"] == T.LIMITS and v["mix_history"][0]["epoch"] == 0
    other = w.tools(names[1]).view(sid)
    assert other["you"]["target"] != v["you"]["target"] or other["you"]["gap"] != v["you"]["gap"]
    assert "target" not in json.dumps({k: x for k, x in v.items() if k not in ("you", "rules")}), "no target outside `you`"
    sizes = []
    for _e in range(1, 6):
        v = w.tools(names[0]).view(sid)
        sizes.append(len(json.dumps(v)))
        for n in names:
            assert w.tools(n).push(sid, {"XY>X": 2, "WX>W": -2})["success"]
        for n in names:
            assert w.tools(n).open(sid)["success"]
        w.clock.advance(20)
    last = w.tools(names[0]).view(sid)
    assert last["owners"]["epoch"] == 5 and set(last["owners"]["pushes"]) == set(names)
    assert max(sizes + [len(json.dumps(last))]) < 9000, sizes
    assert last["epoch"] == 5 and last["phase"] == "secrets" and last["due"] == "tesseract_open"


def test_the_tools_say_what_they_wait_for(w):
    names = agents(w, 3)
    sid = w.table(names)
    w.clock.advance(20)
    t = w.tools(names[0])
    assert t.open(sid)["nothing_due"] and "not sealed" in t.open(sid)["note"]
    assert t.push(sid, {"XY>X": 4})["success"]
    again = t.push(sid, {"XY>X": 4})
    assert not again["success"] and "already sealed" in again["error"]
    assert "waiting_on" in t.open(sid) or "nothing_due" in t.open(sid)
    ov = t.overview()
    assert ov["success"] and [x["session_id"] for x in ov["yours"]] == [sid] and ov["yours"][0]["due"] is None
    stranger = w.register(f"ts{uuid.uuid4().hex[:6]}_s")
    assert not w.tools(stranger).view(sid)["success"]


def test_whats_next_and_the_digest_know_the_table(w):
    names = agents(w, 3)
    sid = w.table(names)
    w.clock.advance(20)
    nxt = w.client.get(f"/agents/{names[0]}/next", headers=w.hdr(names[0])).json()
    tables = nxt["facts"]["tesseract"]["tables"] if "facts" in nxt else nxt["tesseract"]["tables"]
    assert tables[0]["session_id"] == sid and tables[0]["due"] == "seal" and tables[0]["tool"] == "tesseract_push"
    text = json.dumps(nxt)
    assert "tesseract_push" in text or "tesseract" in text
    ranked = nxt.get("next") or nxt.get("items") or []
    if ranked:
        assert any(i.get("tool") == "tesseract" for i in ranked)
    # a finished table is a line of the digest
    sid2, names2, _ = finished(w, 3)
    line = w.client.get("/happening", headers=w.hdr(names2[0]), params={"since": "2020-01-01T00:00:00+00:00"}).json()
    assert line["tesseract"]["ref"].startswith("session:") and line["tesseract"]["untrusted"] is True


# ── the module is a relay module ─────────────────────────────────────────────────────────────────────────

def test_without_the_module_the_tools_are_hidden_the_game_is_not_listed_and_no_table_opens(w, monkeypatch):
    monkeypatch.setenv("EMPORIA_TESSERACT", "0")
    mods = relay_modules.advertise()
    assert "tesseract" not in mods
    hide = relay_modules.tools_to_hide([mods])
    assert set(relay_modules.TESSERACT_TOOLS) <= hide
    assert GAME not in {m["module_type"] for m in w.client.get("/modules").json()["modules"]}
    assert w.client.get("/tesseract").status_code == 404
    assert GAME not in w.client.get("/health").json()["modules"]          # /health agrees with /modules
    names = agents(w, 3)
    from conftest import make_test_commit, signed_session_body
    body = signed_session_body(names[0], {"module_type": GAME, "config": {"players": 3}})
    r = w.client.post("/sessions", json=body, headers=w.hdr(names[0]))
    assert r.status_code == 400 and "does not run" in r.text
    monkeypatch.setenv("EMPORIA_TESSERACT", "1")
    mods = relay_modules.advertise()
    assert mods["tesseract"]["parts"] == ["tables"] and mods["tesseract"]["game"] == GAME
    assert not set(relay_modules.TESSERACT_TOOLS) & relay_modules.tools_to_hide([mods])
    assert GAME in {m["module_type"] for m in w.client.get("/modules").json()["modules"]}
    assert GAME in w.client.get("/health").json()["modules"]
    assert w.client.get("/tesseract").json()["rounds"] == 5
    del make_test_commit


def test_a_relay_with_a_wrong_switch_does_not_start(monkeypatch):
    monkeypatch.setenv("EMPORIA_TESSERACT", "maybe")
    assert any("EMPORIA_TESSERACT" in p for p in relay_modules.problems())


# ── XP and badges: play:v1 and its caps only ─────────────────────────────────────────────────────────────

def xp_of(agent: str) -> dict:
    from emporia.routes.standing import quests_body
    body = quests_body(agent)
    return {"play": body["play"]["xp"], "badges": set(body["badges"]),
            "quests": {q["id"] for q in body["quests"] if q["done"]}}


def test_a_table_with_the_house_is_practice_and_pays_nothing_while_a_real_table_pays_play_xp(w, monkeypatch):
    from tests.ids import house_env
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    names = agents(w, 4)
    house = names[3]
    house_env(monkeypatch, house)
    w.rs.forget_who()
    sid = w.table(names)
    E.play(w, sid, names, {})
    for n in names[:3]:
        assert xp_of(n)["play"] == 0, "a table with the house agent in it counts toward nothing"
        assert not {b for b in xp_of(n)["badges"] if b.startswith("tesseract")}
    # the same table without the house pays each finisher play:v1's 2 XP, and the badge, and no other XP
    real = agents(w, 4)
    sid2 = w.table(real)
    E.play(w, sid2, real, {}, seed=5)
    for n in real:
        got = xp_of(n)
        assert got["play"] == 2 and "tesseract-first" in got["badges"]
        assert not {q for q in got["quests"] if "tesseract" in q}, "no quest row pays XP for a table"
    winner = verdict_of(w, sid2)["winner"]
    assert "tesseract-win" in xp_of(winner)["badges"]
    assert all("tesseract-win" not in xp_of(n)["badges"] for n in real if n != winner)


def test_a_player_who_was_put_out_or_hid_its_secret_earns_no_play_xp(w, monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    names = agents(w, 4)
    sid = w.table(names)
    E.play(w, sid, names, {}, no_open={(names[1], 2)}, no_reveal={names[2]})
    assert xp_of(names[1])["play"] == 0 and xp_of(names[2])["play"] == 0
    assert xp_of(names[0])["play"] == 2 and xp_of(names[3])["play"] == 2
    assert "tesseract-first" not in xp_of(names[1])["badges"] and "tesseract-first" in xp_of(names[0])["badges"]


# ── verify fails on anything changed ─────────────────────────────────────────────────────────────────────

def tampered(w: E.World, sid: str, change):
    """The session as served, the verdict and the ledger, with `change(session, verdict, ledger)` applied to copies."""
    sess = copy.deepcopy(w.client.get(f"/sessions/{sid}", headers=w.hdr(w.state(sid)["participants"][0])).json())
    sess["state"]["data"] = {**w.state(sid)["state"]["data"]}       # the stored record, as verify_session reads it
    sess["state"]["data"] = copy.deepcopy(sess["state"]["data"])
    verdict, ledger = copy.deepcopy(verdict_of(w, sid)), copy.deepcopy(ledger_of(w, sid))
    change(sess, verdict, ledger)
    problems = V.check_table(sess, verdict, ledger, bls=False)
    mod_problem = get_interaction_module(GAME).rand_problem(sess["state"]["data"], sess["participants"], verdict)
    return problems, mod_problem


def test_verify_passes_untouched_and_fails_on_a_changed_pulse_dark_mask_width_target_or_total(w):
    sid, names, _ = finished(w, 4, seed=7)
    p, m = tampered(w, sid, lambda s, v, l: None)
    assert p == [] and m == ""

    def pulse(s, v, l):
        s["state"]["data"]["published"]["2"]["pulses"][3][1] += 1

    def dark(s, v, l):
        d = s["state"]["data"]["published"]["3"]
        d["dark"] = [] if d["dark"] else ["XY>X"]

    def width(s, v, l):
        d = s["state"]["data"]["published"]["4"]["widths"]
        d[5] = (d[5] + 1) % 13

    def mix(s, v, l):
        s["state"]["data"]["published"]["1"]["mix"][0] += 1

    def target(s, v, l):
        t = s["state"]["data"]["targets"]
        who = names[0]
        t[who] = [t[who][1], t[who][0], t[who][2], t[who][3]] if t[who][0] != t[who][1] else [10, 10, 40, 40]

    def total(s, v, l):
        v["points"][names[1]] += 1

    def ledger_total(s, v, l):
        l[0]["total"] += 1

    def moved_push(s, v, l):          # a pushed channel changed after the fact: its seal no longer matches
        mv = next(iter(s["state"]["data"]["opens"]["2"].values()))["move"]["push"]
        mv["XY>X"] = mv.get("XY>X", 0) + 1 if mv.get("XY>X", 0) < 3 else 0

    for name, change in (("pulse", pulse), ("dark mask", dark), ("width", width), ("mix", mix), ("target", target),
                         ("total", total), ("ledger total", ledger_total), ("push", moved_push)):
        problems, mod_problem = tampered(w, sid, change)
        assert problems, f"verify_tesseract.py did not notice a changed {name}"
        if name != "ledger total":          # the ledger is not in the game's own record
            assert mod_problem, f"the module's rand_problem did not notice a changed {name}"


def test_a_secret_that_is_not_the_committed_one_fails_verify(w):
    sid, names, _ = finished(w, 3)

    def swap(s, v, l):
        r = s["state"]["data"]["rand"]["reveals"]
        r[names[0]], r[names[1]] = r[names[1]], r[names[0]]
    problems, _ = tampered(w, sid, swap)
    assert any("revealed secret" in x for x in problems)


# ── the clock ────────────────────────────────────────────────────────────────────────────────────────────

def test_a_round_opens_only_when_its_drand_round_is_published(w):
    names = agents(w, 3)
    sid = w.table(names)
    w.clock.advance(20)
    for n in names:
        assert w.tools(n).push(sid, {"XY>X": 2})["success"]
    for n in names:
        assert w.tools(n).open(sid)["success"]
    # the round the seals named is a few drand rounds ahead: until it is published the next round is not open
    fl = w.flow(sid)
    assert fl["phase"] == "seal" and fl["epoch"] == 2 and fl["opens_at"] > w.clock.t
    v = w.tools(names[0]).view(sid)
    assert v["ready"] is False and "waiting" in v
    mv = {"push": {}}
    seal = get_interaction_module(GAME).fill_move("seal", {"commitment": commit_reveal.commitment(mv, "n" * 64),
                                                          "epoch": 2}, w.state(sid)["state"]["data"], w.clock.t)
    assert "opens when drand round" in raw_act(w, sid, names[0], "seal", seal).text
    w.clock.advance(60)
    assert w.tools(names[0]).view(sid)["ready"] is True


def test_nobody_sealing_ends_every_round_on_the_clock_and_the_table_still_ends(w):
    names = agents(w, 3)
    sid = w.table(names)
    limit = int(w.state(sid)["state"]["data"]["move_time_limit_s"])
    for _ in range(5):
        w.clock.advance(limit + 25)
        w.sweep()
    w.clock.advance(int(w.state(sid)["state"]["data"]["reveal_window_s"]) + 25)
    w.sweep()
    sess = w.state(sid)
    assert sess["status"] == "completed"
    out = get_interaction_module(GAME).is_terminal(SessionState.from_dict(sess["state"]))[1]
    assert out["termination"] == "unrevealed" and out["winner"] is None


def test_the_replay_of_the_stored_actions_gives_the_stored_record(w):
    sid, names, sess = finished(w, 4, seed=9)
    mod = get_interaction_module(GAME)
    served = w.client.get(f"/sessions/{sid}", headers=w.hdr(names[0])).json()
    data = served["state"]["data"]
    assert data["published"]["1"] == data["board"]["done"][0]
    assert mod.derived(sess["state"]["data"])["board"]["done"] == data["board"]["done"]
    assert T.badge_facts(sess["state"]["data"])["finished"] == names


# ── practice tables: opt-in, seat rule lifted, counts toward nothing ─────────────────────────────────────

def test_a_practice_table_of_one_operators_agents_fills_plays_verifies_and_counts_for_nothing(w, monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    a = agents(w, 1)[0]
    names = [a, *(w.register(f"ts{uuid.uuid4().hex[:6]}_p{i}", operator=a) for i in range(2))]
    sid = w.create(a, players=3, practice=True)
    card = w.client.get("/tesseract").json()
    assert next(t for t in card["tables"] if t["session_id"] == sid)["practice"] is True
    assert w.client.get(f"/sessions/{sid}").json().get("config", {}).get("practice") is True
    for seat, n in enumerate(names[1:], start=1):
        assert w.join(sid, n, seat).status_code == 200
    assert w.state(sid)["status"] == "active"
    E.play(w, sid, names, {})
    verified(w, sid)
    assert w.state(sid)["status"] == "completed"
    assert w.state(sid)["state"]["data"]["practice"] is True
    for n in names:
        got = xp_of(n)
        assert got["play"] == 0, "a practice table pays no XP"
        assert not {b for b in got["badges"] if b.startswith("tesseract")}
        assert not {q for q in got["quests"] if "tesseract" in q}
    assert ledger_of(w, sid) == [], "a practice table writes no results row"
    assert T.badge_facts(w.state(sid)["state"]["data"]) == {"finished": [], "winners": [], "seats": 3, "practice": True}
    board = T.view_of(w.state(sid)["state"]["data"])["board"]
    assert board["practice"] is True and "counts toward nothing" in board["practice_note"]
    assert T.is_practice({"practice": True}) and not T.is_practice({"practice": 1}) and not T.is_practice({})


def test_a_table_without_practice_still_refuses_the_second_seat_even_when_it_says_false(w):
    a = agents(w, 1)[0]
    twin = w.register(f"ts{uuid.uuid4().hex[:6]}_twin", operator=a)
    sid = w.create(a, players=3, practice=False)
    r = w.join(sid, twin, 1)
    assert r.status_code == 409 and "one seat per operator" in r.text


def test_practice_must_be_true_or_false(w):
    from conftest import make_test_commit, signed_session_body
    from emporia import wire
    a = agents(w, 1)[0]
    body = signed_session_body(a, {"module_type": GAME, "config": {"players": 3, "practice": "yes"}})
    sid = wire.session_id_for(body["nonce"])
    r = w.client.post("/sessions", json={**body, "rand_commit": make_test_commit(sid, a, 0)}, headers=w.hdr(a))
    assert r.status_code == 400 and "practice" in r.text


def test_a_relay_that_cannot_fill_a_real_table_offers_a_practice_one(w, monkeypatch):
    from emporia import operators
    from emporia import next_steps
    from emporia.routes import tesseract as route
    a = agents(w, 1)[0]
    monkeypatch.setattr(operators, "of_agents", lambda conn: {a: "op1", "b": "op1", "c": "op2"})
    fact = route.next_fact(a)
    assert fact == {"tables": [], "start_practice": True}
    items = next_steps._tesseract(fact)
    assert items and items[0]["tool"] == "create_session" and items[0]["args"]["config"]["practice"] is True
    monkeypatch.setattr(operators, "of_agents", lambda conn: {a: "op1", "b": "op2", "c": "op3"})
    assert route.next_fact(a) is None


def _practice_table(w, tag_n: int = 3):
    a = agents(w, 1)[0]
    names = [a, *(w.register(f"ts{uuid.uuid4().hex[:6]}_q{i}", operator=a) for i in range(tag_n - 1))]
    sid = w.create(a, players=tag_n, practice=True)
    for seat, n in enumerate(names[1:], start=1):
        assert w.join(sid, n, seat).status_code == 200
    E.play(w, sid, names, {})
    assert w.state(sid)["status"] == "completed"
    return sid, names


def test_a_practice_result_reaches_no_standing_record_sheet_rating_or_settlement(w, monkeypatch):
    """A control table (real, four operators) shows each path DOES count a played table; the practice table, played to
    the end by one operator's three agents, shows up in none of them."""
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    real_sid, real, _ = finished(w, 4)
    sid, names = _practice_table(w)
    # leaderboard: games played and wins
    # the board is cached for a window and cut at `limit` (25 by default); earlier tables of the same database fill it
    from emporia.routes import standing as ST0
    ST0._PUBLIC_CACHE.clear()
    board = {r["agent_id"]: r for r in w.client.get(
        "/leaderboard", params={"module_type": GAME, "limit": 200}).json()["standings"]}
    assert all(n in board for n in real), "control: a real table is on the leaderboard"
    assert not set(names) & set(board), "a practice table is no contest: nobody is ranked for it"
    # settlements: a practice table writes no win
    with rs.DB_LOCK, rs.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM settlements WHERE session_id = ?", (real_sid,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM settlements WHERE session_id = ?", (sid,)).fetchone()[0] == 0
    # results list, the agent record and the sheet (Craft, Insight, Trade, Trust read the same finished games)
    for n in names:
        assert w.client.get("/results", params={"agent_id": n}).json()["results"] == []
        assert w.client.get(f"/agents/{n}/record").json()["games"]["finished"] == 0
        sheet = w.client.get(f"/agents/{n}/sheet").json()
        assert sheet["sessions_counted"] == 0 and sid not in json.dumps(sheet.get("records_hash", ""))
        assert w.client.get(f"/agents/{n}/quests").json()["play"]["xp"] == 0
    for n in real:
        assert w.client.get(f"/agents/{n}/record").json()["games"]["finished"] == 1
        assert w.client.get(f"/agents/{n}/sheet").json()["sessions_counted"] == 1
    # Craft ladder and the season
    for path in ("/ratings", "/season", "/season/board"):
        text = w.client.get(path, params={"module_type": GAME} if path == "/ratings" else {}).text
        assert not any(n in text for n in names), f"{path} names a practice player"
    from emporia.routes import standing as ST
    assert sid not in {g["session_id"] for g in ST._craft_games()}
    assert real_sid in {g["session_id"] for g in ST._craft_games()}


def test_a_practice_end_names_no_winner_to_followers_or_watchers(w):
    import asyncio

    from emporia import notices
    sid, names = _practice_table(w)
    sess = w.state(sid)
    sent: list[dict] = []

    async def spy(agent, event):
        sent.append(event)
    with rs.DB_LOCK, rs.get_db() as conn:
        notices.watch(conn, "someone_else", sid, True)
    import unittest.mock as m
    with m.patch.object(rs, "broadcast_to_agent", spy):
        asyncio.run(notices.on_game_finished(sid, sess, {"winner": names[0], "outcome_type": "won"}))
    assert sent and all(e["winner"] == "" and e["practice"] is True for e in sent)
    with rs.DB_LOCK, rs.get_db() as conn:
        assert notices.following_activity(conn, names[0])["items"] == []


def test_a_practice_table_is_marked_in_the_waiting_view_the_tools_and_whats_next(w):
    a = agents(w, 1)[0]
    mates = [w.register(f"ts{uuid.uuid4().hex[:6]}_m{i}", operator=a) for i in range(2)]
    sid = w.create(a, players=3, practice=True)
    waiting = w.tools(a).view(sid)
    assert waiting["practice"] is True and "counts toward nothing" in waiting["practice_note"] and "waiting" in waiting
    open_row = next(t for t in w.tools(mates[0]).overview()["open"] if t["session_id"] == sid)
    assert open_row["practice"] is True
    for seat, n in enumerate(mates, start=1):
        assert w.join(sid, n, seat).status_code == 200
    nxt = w.client.get(f"/agents/{a}/next", headers=w.hdr(a)).json()
    assert nxt["tesseract"]["tables"][0]["practice"] is True
    from emporia import next_steps
    step = next(i for i in next_steps._tesseract(nxt["tesseract"]) if i["kind"] != "guided")
    assert "practice" in step["why"] and "counts toward nothing" in step["why"]
    E.play(w, sid, [a, *mates], {})
    done = w.tools(a).view(sid)
    assert done["practice"] is True and done["result"]["practice"] is True
    assert "counts toward nothing" in done["result"]["practice_note"]
    assert w.tools(a).open(sid)["practice"] is True      # nothing due on a finished table, still labeled


def test_practice_tables_have_their_own_small_cap_and_sit_after_real_tables_on_the_card(w, monkeypatch):
    monkeypatch.setenv("EMPORIA_ALLOWANCE", "1")
    from emporia.standing import PRACTICE_WAITING_CAP
    a = agents(w, 1)[0]
    real = w.create(a, players=4)
    made = [w.create(a, players=3, practice=True) for _ in range(PRACTICE_WAITING_CAP)]
    from conftest import make_test_commit, signed_session_body
    from emporia import wire
    body = signed_session_body(a, {"module_type": GAME, "config": {"players": 3, "practice": True}})
    sid = wire.session_id_for(body["nonce"])
    r = w.client.post("/sessions", json={**body, "rand_commit": make_test_commit(sid, a, 0)}, headers=w.hdr(a))
    assert r.status_code in (400, 429) and "practice tables" in r.text, r.text
    ids = [t["session_id"] for t in w.client.get("/tesseract").json()["tables"]]
    assert ids.index(real) < min(ids.index(m) for m in made), "real tables come first"


def test_practice_true_on_a_game_without_practice_tables_does_not_skip_the_open_games_cap(w, monkeypatch):
    monkeypatch.setenv("EMPORIA_ALLOWANCE", "1")
    from emporia.routes import sessions as sessions_route
    from emporia.routes import standing as ST
    a = agents(w, 1)[0]
    cap = ST.allowance_of(a)["limits"]["open_games"]
    other = "emporia:chess:v1"
    from emporia.module_sdk import MODULE_REGISTRY
    if other not in MODULE_REGISTRY:
        other = next(t for t, c in MODULE_REGISTRY.items() if not getattr(c, "PRACTICE_TABLES", False)
                     and t not in ("emporia:service:v1", "emporia:research:v1", "emporia:code-review:v1"))
    # a practice flag on a module that ignores it is not exempt; Tesseract's is
    import emporia.routes.standing as S
    real_usage = S.usage
    S.usage = lambda conn, agents: {**real_usage(conn, agents), "open_games": cap}
    try:
        assert "games open" in sessions_route._open_games_limit(a, other, {"practice": True})
        assert sessions_route._open_games_limit(a, GAME, {"practice": True}) == ""
    finally:
        S.usage = real_usage
