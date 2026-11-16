"""Real wire fixtures for the dashboard's Tesseract reader (`dashboard/src/tesseract.ts`): tables played through the relay's
test client and dumped as the viewer receives them (`GET /sessions/{id}` and `GET /sessions/{id}/actions`).

`dashboard/src/fixtures/tesseract/*.json` are committed. This test (1) replays each finished one with the module's own
rules, so a fixture can never drift from the rules, and (2) rewrites them when `EMPORIA_WRITE_TESSERACT_FIXTURES=1`:

    EMPORIA_WRITE_TESSERACT_FIXTURES=1 EMPORIA_NO_DOTENV=1 PYTHONPATH=src .venv/bin/python -m pytest tests/test_tesseract_fixtures.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import e2e_tesseract as E

from emporia import relay_server as rs
from emporia.module_sdk import get_interaction_module
from emporia.modules import tesseract as T

DIR = Path(__file__).parent.parent / "dashboard" / "src" / "fixtures" / "tesseract"
WRITE = os.environ.get("EMPORIA_WRITE_TESSERACT_FIXTURES") == "1"


@pytest.fixture
def w():
    world = E.World(TestClient(rs.app, client=("127.0.0.1", 51781)))
    yield world
    world.close()


def _dump(w, sid, who):
    served = w.client.get(f"/sessions/{sid}", headers=w.hdr(who))
    acts = w.client.get(f"/sessions/{sid}/actions", headers=w.hdr(who))
    assert served.status_code == 200 and acts.status_code == 200, (served.text, acts.text)
    session, actions = served.json(), acts.json()["actions"]
    # what the dashboard reads, not what it never draws: the pull table (the full view's, T2) and each action's copy of the
    # state it produced
    ((session.get("state") or {}).get("data") or {}).get("board", {}).pop("pull", None)
    for a in actions:
        (a.get("result") or {}).pop("new_state", None)
    return {"session": session, "actions": actions}


def _write(name, fx):
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / f"{name}.json").write_text(json.dumps(fx, indent=1, sort_keys=True) + "\n")


@pytest.mark.skipif(not WRITE, reason="set EMPORIA_WRITE_TESSERACT_FIXTURES=1 to regenerate")
def test_write_the_fixtures(w):
    names = [w.register(f"fx_{i}") for i in range(4)]
    # a finished table, with an out seat and one that sat a round out, so the record shows both
    sid = w.table(names)
    E.play(w, sid, names, {names[2]: "random", names[3]: "idle"}, seed=4, say=True,
           sit_out={(names[0], 3)}, no_open={(names[3], 4)})
    _write("table", _dump(w, sid, names[0]))
    # a running one: two rounds opened, the third sealed by two seats
    sid = w.table([w.register(f"fy_{i}") for i in range(4)])
    live = [n for n in w.state(sid)["participants"]]
    for e in (1, 2):
        w.clock.advance(20)
        for n in live:
            assert w.tools(n).push(sid, {"XY>X": 4, "WX>W": -2} if e == 1 else {"YZ>Z": 3})["success"]
        for n in live:
            assert w.tools(n).open(sid)["success"]
    w.clock.advance(20)
    for n in live[:2]:
        assert w.tools(n).push(sid, {"XZ>Z": 2})["success"]
    _write("mid", _dump(w, sid, live[0]))
    # a waiting one: two of four seated
    waiting = w.create(w.register("fz_0"), players=4)
    assert w.join(waiting, w.register("fz_1"), 1).status_code == 200
    _write("waiting", _dump(w, waiting, "fz_0"))


@pytest.mark.parametrize("name", ["table", "mid", "waiting"])
def test_the_committed_fixtures_are_what_the_rules_replay(name):
    fx = json.loads((DIR / f"{name}.json").read_text())
    sess = fx["session"]
    data = sess["state"]["data"]
    assert sess["module_type"] == T.GAME_ID
    assert [a["action_type"] for a in fx["actions"]] == [a["action_type"] for a in fx["actions"]]
    assert all("commitment" in a["payload"] or "move" in a["payload"] or "s" in a["payload"] for a in fx["actions"])
    if name == "table":
        assert sess["status"] == "completed"
        assert get_interaction_module(T.GAME_ID).rand_problem(data, sess["participants"], None) == ""
        assert len([k for k in data["published"] if k != "opening"]) == 5
        assert fx["actions"][-1]["result"]["artifacts"]["result"]["points"]
    elif name == "mid":
        assert sess["status"] == "active" and data["board"]["epoch"] == 3 and data["board"]["phase"] == "seal"
    else:
        assert sess["status"] == "waiting" and len(sess["participants"]) == 2 and fx["actions"] == []
