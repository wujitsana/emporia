"""The game's first name, `emporia:sluice:v1`, stays registered as a read-only alias of Spillway: a session stored
under it still displays, exports and verifies, and a new one is refused with a message naming Spillway."""
import copy
import json

from tests import test_spillway as T

from emporia import game_export
from emporia.module_sdk import MODULE_REGISTRY, get_interaction_module
from emporia.modules import spillway as S
from emporia.verify import verify_game

OLD = "emporia:sluice:v1"
client, finished = T.client, T.finished     # the fixtures, shared with the Spillway tests


def test_the_old_type_is_an_alias_of_the_same_rules_and_is_retired_for_spillway():
    old, new = MODULE_REGISTRY[OLD], MODULE_REGISTRY["emporia:spillway:v1"]
    assert issubclass(old, new) and old.RETIRED_FOR == new.MODULE_TYPE and new.RETIRED_FOR is None
    assert S.canonical_type(OLD) == new.MODULE_TYPE and S.canonical_type("emporia:chess:v1") == "emporia:chess:v1"
    assert get_interaction_module(OLD).__class__ is old


def test_creating_a_sluice_game_is_refused_naming_spillway(finished):
    c, _, agents, _ = finished
    r = c.post("/sessions", json={"module_type": OLD, "config": {}}, headers=T._hdr(agents[0]))
    assert r.status_code == 400 and "Spillway" in r.json()["detail"] and "emporia:spillway:v1" in r.json()["detail"]


def test_a_finished_game_stored_under_the_old_type_reads_exports_and_verifies(finished):
    c, sid, _, record = finished
    old = copy.deepcopy(record)
    old["session"]["module_type"] = OLD
    report = verify_game(**old)
    assert report["verdict"] == "ok", [f for f in report["findings"] if f["status"] != "ok"]
    r = c.get("/games/export", params={"module": "emporia:spillway:v1", "limit": 200})
    line = next(json.loads(x) for x in r.text.splitlines() if json.loads(x)["session_id"] == sid)
    line["module"] = OLD
    rep = game_export.verify_line(line)
    statuses = {f["check"]: f["status"] for f in rep["findings"]}
    # the relay's signed result names the new type, so only that check differs: this edit stands in for a game
    # that was signed under the old name
    assert statuses["replay"] == "ok" and statuses["beacons_bls"] == "ok"
    assert [f["check"] for f in rep["findings"] if f["status"] == "fail"] in ([], ["referee"]), rep["findings"]
