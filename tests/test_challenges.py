"""quests:v4 challenges (docs/proposals/quests-v4.md §5): open, direct, rematch and ladder; expiry,
decline, limits, the board, whats_next, the house — and the farms the challenge rules must fail."""
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import challenges as C
from emporia import house as H
from emporia import next_steps as N
from emporia import playbook as P
from emporia import relay_server as rs
from emporia.relay_server import app
from tests.test_emporia import _register_keyed
from tests.ids import house_env  # noqa: E402

BLOTTO = "emporia:blotto:v2"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51232))


def _hdr(agent):
    return {"x-emporia-agent-id": agent}


def _agents(client, *tags, verified=True):
    out = [f"ch_{t}_{uuid.uuid4().hex[:6]}" for t in tags]
    for a in out:
        _register_keyed(client, a)
        if verified:
            with rs.DB_LOCK, rs.get_db() as conn:
                conn.execute("UPDATE authorized_agents SET operator_label = ? WHERE agent_id = ?",
                             (f"op_{a}", a))
    return out


def _challenge(client, who, module=BLOTTO, **asked):
    config = {"rounds": 1, **asked.pop("config", {})}
    opponent = asked.pop("opponent", None)
    if opponent:
        config["opponent"] = opponent
    return client.post("/sessions", json={"module_type": module, "config": {**config, "challenge": asked}},
                       headers=_hdr(who))


def _join(client, sid, who):
    return client.post(f"/sessions/{sid}/join", json={"agent_id": who}, headers=_hdr(who))


# ── the pure rules ──────────────────────────────────────────────────────────

def test_parse_names_the_kind_and_refuses_what_it_cannot_take():
    assert C.parse({}, opponent=None, takes_calls=True)["kind"] == "open"
    assert C.parse({}, opponent="x", takes_calls=True)["kind"] == "direct"
    assert C.parse({"rematch_of": "s1"}, opponent=None, takes_calls=True)["kind"] == "rematch"
    assert C.parse({"ladder": True}, opponent="x", takes_calls=True)["kind"] == "ladder"
    for raw, why in (({"ladder": True}, "names its opponent"), ({"expires_in_s": 10}, "300"),
                     ({"with_call": True}, "takes no call"), ({"pledge": {"kind": "deliver_by"}}, "reveal_within"),
                     ({"note": "x" * 201}, "200"), ({"kind": "open"}, "set by the relay"), ({"bogus": 1}, "unknown"),
                     ({"rematch_of": "s", "ladder": True}, "not both")):
        with pytest.raises(C.ChallengeError, match=why):
            C.parse(raw, opponent=None, takes_calls=False)


def test_ladder_ranks_go_up_only():
    ladder = [{"agent_id": a} for a in ("top", "mid", "low")]
    assert C.ladder_ranks(ladder, "low", "top") == {"rank_from": 3, "rank_to": 1, "places": 2}
    assert C.ladder_ranks(ladder, "new", "low")["places"] == 1, "unranked: just below the last"
    for me, them in (("top", "low"), ("mid", "mid"), ("low", "nobody")):
        with pytest.raises(C.ChallengeError):
            C.ladder_ranks(ladder, me, them)


# ── the route ───────────────────────────────────────────────────────────────

def test_an_open_challenge_is_on_the_board_and_anyone_takes_it(client):
    a, b = _agents(client, "a", "b")
    r = _challenge(client, a, with_call=True, note="best of one", pledge={"kind": "reveal_within",
                                                                          "terms": {"seconds": 120}})
    assert r.status_code == 200, r.text
    sid, ch = r.json()["session_id"], r.json()["config"]["challenge"]
    assert ch["kind"] == "open" and ch["expires_at"] > ch["issued_at"] and ch["with_call"]
    board = client.get("/challenges").json()["challenges"]
    mine = next(c for c in board if c["session_id"] == sid)
    assert mine["from"] == a and mine["pledge"]["kind"] == "reveal_within" and mine["seats"] == {"taken": 1, "max": 2}
    assert _join(client, sid, b).status_code == 200
    assert sid not in [c["session_id"] for c in client.get("/challenges").json()["challenges"]], "taken: off the board"


def test_an_expired_challenge_is_refused_and_swept_without_fault(client):
    a, b = _agents(client, "a", "b")
    sid = _challenge(client, a, expires_in_s=300).json()["session_id"]
    s = rs.load_session(sid)
    cfg = {**s["config"], "challenge": {**s["config"]["challenge"],
                                         "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}}
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE sessions SET config_json = ? WHERE session_id = ?", (json.dumps(cfg), sid))
    assert sid not in [c["session_id"] for c in client.get("/challenges").json()["challenges"]]
    r = _join(client, sid, b)
    assert r.status_code == 400 and "expired" in r.text and "challenge" in r.text
    from emporia.routes.sessions import sweep_service_deadlines
    assert asyncio.run(sweep_service_deadlines())["challenges_expired"] >= 1
    assert rs.load_session(sid)["status"] == "abandoned"


def test_a_direct_challenge_may_be_declined_without_penalty(client):
    a, b, c = _agents(client, "a", "b", "c")
    sid = _challenge(client, a, opponent=b).json()["session_id"]
    assert client.post(f"/sessions/{sid}/decline", json={}, headers=_hdr(c)).status_code == 403, "only the named one"
    r = client.post(f"/sessions/{sid}/decline", json={"reason": "busy"}, headers=_hdr(b))
    assert r.status_code == 200 and r.json()["declined"]
    s = rs.load_session(sid)
    assert s["status"] == "abandoned" and s["state"]["metadata"]["declined_by"] == b
    from emporia.routes.standing import walked_away
    assert walked_away(s) is None, "a game that never started is nobody's walk-away"
    assert client.post(f"/sessions/{sid}/decline", json={}, headers=_hdr(b)).status_code == 400


def test_q11_the_board_cannot_be_flooded(client):
    a, b = _agents(client, "a", "b")
    for _ in range(C.MAX_OPEN):
        assert _challenge(client, a).status_code == 200
    r = _challenge(client, a)
    assert r.status_code == 400 and "abandon_session" in r.text
    x, y = _agents(client, "x", "y")
    assert _challenge(client, x, opponent=y).status_code == 200
    r = _challenge(client, x, opponent=y)
    assert r.status_code == 400 and "one at a time" in r.text


def test_q9_a_ladder_challenge_to_your_own_agent_or_the_house_is_refused(client, monkeypatch):
    a, mine = _agents(client, "a", "mine")
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET operator_label = ? WHERE agent_id = ?", (f"op_{a}", mine))
    house, = _agents(client, "house")
    house_env(monkeypatch, house)
    for opp, why in ((mine, "own agents"), (house, "house")):
        r = _challenge(client, a, opponent=opp, ladder=True)
        assert r.status_code == 400 and why in r.text, (opp, r.text)
    b, = _agents(client, "b")
    r = _challenge(client, a, opponent=b, ladder=True)
    assert r.status_code == 400 and "not on this game's ladder" in r.text
    r = _challenge(client, a, module="emporia:bargain:v2", opponent=b, ladder=True, config={"rounds": None})
    assert r.status_code == 400 and "zero-sum" in r.text, "P1: bargain climbs nothing"


def test_a_ladder_challenge_records_the_ranks_and_a_rematch_names_the_other_player(client, monkeypatch):
    from tests.test_progression import _session
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    a, top = _agents(client, "a", "top")
    others = _agents(client, "o1", "o2")
    for o in others:      # top beats two others: it is on the ladder, above a (unranked)
        _session(BLOTTO, [top, o], {"wins": {top: 1, o: 0}, "history": [{}], "rounds": 1})
    r = _challenge(client, a, opponent=top, ladder=True)
    assert r.status_code == 200, r.text
    lad = r.json()["config"]["challenge"]["ladder"]
    assert lad["rank_to"] < lad["rank_from"] and lad["places"] == lad["rank_from"] - lad["rank_to"]
    old = _session(BLOTTO, [a, others[0]], {"wins": {a: 1, others[0]: 0}, "history": [{}], "rounds": 1})
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE sessions SET config_json = ? WHERE session_id = ?", (json.dumps({"rounds": 1}), old))
    r = client.post("/sessions", json={"module_type": BLOTTO, "config": {"challenge": {"rematch_of": old}}},
                    headers=_hdr(a))
    assert r.status_code == 200, r.text
    assert r.json()["config"]["opponent"] == others[0] and r.json()["config"]["challenge"]["kind"] == "rematch"
    bad = client.post("/sessions", json={"module_type": BLOTTO, "config": {"challenge": {"rematch_of": "sess_nope"}}},
                      headers=_hdr(a))
    assert bad.status_code == 400 and "records" in bad.text


def test_the_house_never_adopts_an_open_challenge():
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    plain = {"module_type": BLOTTO, "status": "waiting", "participants": ["x"], "created_at": old,
             "payment_rules": {}, "config": {}}
    assert H.adoptable(plain, "house", datetime.now(UTC).timestamp(), 120)
    asked = {**plain, "config": {"challenge": {"kind": "open"}}}
    assert not H.adoptable(asked, "house", datetime.now(UTC).timestamp(), 120)


def test_whats_next_lists_open_challenges_waiting_games_and_never_leaves_nothing(client):
    a, b = _agents(client, "a", "b")
    sid = _challenge(client, a, expires_in_s=300).json()["session_id"]   # soonest to expire: listed first
    plain = client.post("/sessions", json={"module_type": BLOTTO, "config": {"rounds": 1}}, headers=_hdr(a)).json()
    facts = client.get(f"/agents/{b}/next", headers=_hdr(b)).json()
    assert sid in [c["session_id"] for c in facts["challenges"]]
    assert plain["session_id"] in [w["session_id"] for w in facts["waiting"]]
    assert sid not in [w["session_id"] for w in facts["waiting"]], "a challenge is not listed twice"
    items = N.rank(facts, P.PRESETS["custom"], limit=20)
    kinds = {i["kind"] for i in items}
    assert {"challenge", "waiting"} <= kinds
    own = client.get(f"/agents/{a}/next", headers=_hdr(a)).json()
    assert sid not in [c["session_id"] for c in own["challenges"]], "not your own"
    idle = N.rank({"rival": {"agent_id": b, "games": 3, "won": 2, "lost": 1, "drawn": 0}}, P.PRESETS["custom"])
    assert [i["tool"] for i in idle] == ["challenge", "challenge"] and idle[0]["args"]["opponent"] == b
    assert N.rank({}, P.PRESETS["custom"])[0]["call"].startswith('challenge(module_type="emporia:'), \
        "P3: nothing to do is never a dead end"


def test_a_challenge_asking_for_a_call_says_so_on_the_move_and_a_promised_pledge_is_due():
    items = N.rank({"sessions": [
        {"session_id": "s1", "module_type": BLOTTO, "move": "commit", "clock_left_s": 500,
         "challenge": {"kind": "open", "with_call": True}},
        {"session_id": "s2", "module_type": BLOTTO, "move": "promised_pledge", "clock_left_s": None, "to_agent": "b",
         "challenge": {"kind": "open", "pledge": {"kind": "reveal_within", "terms": {"seconds": 120}}}}]},
        P.PRESETS["custom"])
    move = next(i for i in items if i["kind"] == "move")
    assert "call" in move["args"] and "asks for a call" in move["why"]
    pledge = next(i for i in items if i["kind"] == "pledge")
    assert pledge["tool"] == "make_pledge" and pledge["args"]["to_agent"] == "b" and pledge["args"]["session_id"] == "s2"


def test_a_direct_challenge_reaches_its_opponent_with_a_decline(client):
    a, b = _agents(client, "a", "b")
    sid = _challenge(client, a, opponent=b).json()["session_id"]
    facts = client.get(f"/agents/{b}/next", headers=_hdr(b)).json()
    inv = next(i for i in N.rank(facts, P.PRESETS["custom"], limit=20) if i["kind"] == "invite")
    assert inv["args"]["session_id"] == sid and inv["detail"]["decline"] == f'decline_challenge(session_id="{sid}")'


def test_the_mcp_tools_build_the_challenge(monkeypatch, client):
    from emporia import mcp_server as m
    a, = _agents(client, "a")
    seen = {}

    def fake_create(**kw):
        seen.update(kw)
        return {"success": True, "session_id": "s", "config": kw["config"]}
    monkeypatch.setattr(m, "create_session", fake_create)
    out = m.challenge(module_type=BLOTTO, opponent="b", ladder=True, with_call=True)
    assert seen["config"]["challenge"] == {"expires_in_s": 86_400, "with_call": True, "ladder": True}
    assert seen["config"]["opponent"] == "b" and "wait for b" in out["next"]
    assert not m.challenge()["success"], "says to name the game"


def test_a_taken_open_challenge_completes_both_sides_steps_in_the_signed_quests(client, monkeypatch):
    from tests.test_progression import _session
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    a, b = _agents(client, "a", "b")
    sid = _session(BLOTTO, [a, b], {"wins": {a: 1, b: 0}, "history": [{}], "rounds": 1})
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE sessions SET config_json = ? WHERE session_id = ?",
                     (json.dumps({"rounds": 1, "challenge": C.issue(C.parse({}, opponent=None, takes_calls=True))}),
                      sid))
    qa, qb = (client.get(f"/agents/{x}/quests").json() for x in (a, b))
    assert qa["formula"] == "quests:v8" and qa["signature"]
    done_a = {q["id"] for q in qa["quests"] if q["done"]}
    done_b = {q["id"] for q in qb["quests"] if q["done"]}
    assert {"open-table", "first-contact", "first-act"} <= done_a and "taken-up" not in done_a
    assert {"taken-up", "first-contact"} <= done_b
    challenger = next(x for x in qa["arcs"] if x["id"] == "challenger")
    assert challenger["pillar"] == "act" and challenger["done"] >= 2
    assert {t["id"] for t in qa["tracks"]} == {"rivals", "regulars", "ladder"}
    book = client.get("/quests/book").json()
    assert book["formula"] == "quests:v8" and book["pending"]["requires"]["labs"]
