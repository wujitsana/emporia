"""Real wire fixtures for the dashboard's Spillway reader (`dashboard/src/spillway.ts`): sessions played through the relay's
test client and dumped as the viewer receives them (`GET /sessions/{id}` and `GET /sessions/{id}/actions`).

`dashboard/src/fixtures/spillway/*.json` are committed. This test (1) replays each with the module's own rules, so a
fixture can never drift from the rules, and (2) rewrites them when `EMPORIA_WRITE_SPILLWAY_FIXTURES=1`:

    EMPORIA_WRITE_SPILLWAY_FIXTURES=1 EMPORIA_NO_DOTENV=1 .venv/bin/python -m pytest tests/test_spillway_fixtures.py
"""
import json
import os
import random
from pathlib import Path

import pytest
from test_spillway import SPILLWAY, M, _drop, _game, _hdr, _play, _served  # noqa: F401

from fastapi.testclient import TestClient

from emporia import relay_server as rs
from emporia.module_sdk import SessionState
from emporia.modules import spillway as S

@pytest.fixture
def client():
    return TestClient(rs.app, client=("127.0.0.1", 51241))


DIR = Path(__file__).parent.parent / "dashboard" / "src" / "fixtures" / "spillway"
WRITE = os.environ.get("EMPORIA_WRITE_SPILLWAY_FIXTURES") == "1"


def _dump(client, sid, who):
    served = _served(client, sid, who)
    r = client.get(f"/sessions/{sid}/actions", headers=_hdr(who))
    assert r.status_code == 200, r.text
    return {"session": served, "actions": r.json()["actions"]}


def _write(name, fx):
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / f"{name}.json").write_text(json.dumps(fx, indent=1, sort_keys=True) + "\n")


@pytest.mark.skipif(not WRITE, reason="set EMPORIA_WRITE_SPILLWAY_FIXTURES=1 to regenerate")
def test_write_the_fixtures(client, monkeypatch):
    for name, want, rule in (("four", "four", "second"), ("threes", "threes", "second"),
                             ("level", "level", "second"), ("coin", "coin", "coin")):
        monkeypatch.setattr(S, "LEVEL_RULE", rule)
        for seed in range(1, 300):          # a whole game, every drop sent through the relay
            sid, agents = _game(client, {"drops": 5})
            if _play(client, sid, agents, random.Random(seed))["state"]["data"]["ended"]["how"] == want:
                break
        else:
            raise AssertionError(f"no {want} game found")
        _write(name, _dump(client, sid, agents[0]))
    monkeypatch.setattr(S, "LEVEL_RULE", "second")
    # a live game after two drops whose next gate round has not been stored yet
    sid, agents = _game(client)
    for _ in range(2):
        d = _served(client, sid, agents[0])["state"]["data"]
        assert _drop(client, sid, d["to_move"], d["open"][0]).status_code == 200
    s = rs.load_session(sid)
    last = s["state"]["data"]["moves"][-1]["round"]
    d = s["state"]["data"]
    d["rand"]["beacons"].pop(str(last), None)
    rs.save_session(session_id=sid, module_type=s["module_type"], config=s["config"], payment_rules=s["payment_rules"],
                    state=SessionState.from_dict({**s["state"], "data": d}),
                    current_agent=s["current_agent"], step_number=s["step_number"], status="active",
                    participants=s["participants"], created_at=s["created_at"], updated_at=s["updated_at"])
    from emporia.routes import beacon as beacon_route
    monkeypatch.setattr(beacon_route, "signature", lambda rnd: None)
    served = _dump(client, sid, agents[0])
    assert served["session"]["state"]["data"]["gate_ready"] is False
    _write("mid", served)
    sid, agents = _game(client, ahead=4)
    _write("pre", _dump(client, sid, agents[0]))      # (the draw is still not published: signature() is None)


def _load(name):
    return json.loads((DIR / f"{name}.json").read_text())


@pytest.mark.parametrize("name", ("four", "threes", "level", "coin"))
def test_a_finished_fixture_replays_under_the_rules(name):
    fx = _load(name)
    s, acts = fx["session"], fx["actions"]
    data = s["state"]["data"]
    assert s["status"] == "completed" and data["ended"]["how"] == (name if name != "level" else "level")
    saved = S.LEVEL_RULE
    try:
        S.LEVEL_RULE = "coin" if name == "coin" else "second"
        assert M().rand_problem(data, s["participants"], None) == ""
        res = S.verdict(data)
    finally:
        S.LEVEL_RULE = saved
    assert res["termination"] == data["ended"]["how"] and res["winner"] == data["ended"]["winner"]
    final = acts[-1]["result"]["artifacts"]["result"]
    assert final["termination"] == res["termination"] and final["winner"] == res["winner"]


def test_the_pre_deal_and_mid_game_fixtures_are_what_they_claim():
    pre = _load("pre")["session"]["state"]["data"]
    assert pre["opening"] is None and pre["moves"] == [] and "gate" not in pre
    mid = _load("mid")["session"]["state"]["data"]
    assert mid["gate_ready"] is False and mid["gate"] is None and mid["open"] == [] and len(mid["moves"]) == 2
