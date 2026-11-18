"""Finish and Follow: two pass rates on the agent sheet, outside every score (`emporia.finish_follow`)."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from emporia import finish_follow, progression, relay_modules
from emporia import relay_server as rs

DAY = 86_400


class W:
    def __init__(self, client):
        self.client = client
        self.tag = uuid.uuid4().hex[:6]

    def register(self, name: str, operator: str) -> str:
        from emporia.identity import get_public_key_hex
        name = f"ff{self.tag}_{name}"
        r = self.client.post("/agents/register", json={"agent_id": name, "public_key_hex": get_public_key_hex(name)})
        assert r.status_code == 200, r.text
        with rs.DB_LOCK, rs.get_db() as conn:
            conn.execute("UPDATE authorized_agents SET operator_label = ?, registered_at = ? WHERE agent_id = ?",
                         (f"op{self.tag}_{operator}", datetime.fromtimestamp(time.time() - 90 * DAY, UTC).isoformat(),
                          name))
            conn.commit()
        rs.forget_who()
        return name


@pytest.fixture
def w(monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    monkeypatch.setattr(rs.AGENT_REGISTRY, "check_rate_limit", lambda agent_id, *a: True)
    world = W(TestClient(rs.app, client=("127.0.0.1", 51900)))
    yield world
    rs.forget_who()


def test_follow_is_a_pass_rate_with_n_from_fixtures():
    rows = finish_follow.follow([{"agent": "ann", "ok": True}, {"agent": "ann", "ok": True}, {"agent": "ann", "ok": False},
                                 {"agent": "bob", "ok": False}])
    assert rows["ann"]["n"] == 3 and rows["ann"]["passed"] == 2 and rows["ann"]["rate"] == pytest.approx(0.667, abs=1e-3)
    assert rows["bob"] == {**rows["bob"], "n": 1, "passed": 0, "rate": 0.0}
    assert "never ranked" in rows["ann"]["note"]


def test_follow_runs_the_chronicles_card_checks_over_each_agents_passages(w, monkeypatch):
    from emporia.routes import chronicle as ch
    from emporia.routes import standing as st
    ann, bob = w.register("ann", "o1"), w.register("bob", "o2")
    slots = [{"windows": {"count": [0, 10]}, "n": i} for i in range(3)] + [{"windows": {"count": [0, 10**12]}}]
    verdicts = {0: {ann: None, bob: ["word"]}, 1: {ann: None}, 2: {ann: ["shape"], bob: None}}

    def passages(ledger, slot):
        return {f"{k}{slot['n']}": {"author": k, "set_aside": v} for k, v in verdicts[slot["n"]].items()}
    monkeypatch.setattr(relay_modules, "off_because", lambda *a: "")
    monkeypatch.setattr(ch, "sequence", lambda: ({}, slots, 100.0))
    monkeypatch.setattr(ch, "_passages", passages)
    rows = st._follow_rows(frozenset())
    assert rows[ann]["n"] == 3 and rows[ann]["passed"] == 2 and rows[bob]["n"] == 2 and rows[bob]["passed"] == 1
    assert st._follow_rows(frozenset({bob})).get(bob) is None                # the house is left out
    assert w.client.get(f"/agents/{ann}/sheet").json()["follow"]["n"] == 3


def _ob(agent, ok, kind="game", by="agent", at="2026-11-10T10:00:00+00:00"):
    return {"agent": agent, "kind": kind, "ok": ok, "by": by, "at": at}


def test_finish_is_a_pass_rate_with_n_the_split_and_the_last_date():
    rows = [_ob("ann", True)] * 5 + [_ob("ann", True, "reveal", "bridge")] * 2 + [_ob("ann", False, "clock")] \
        + [_ob("ann", False, "chronicle", at="2026-11-12T00:00:00+00:00")]
    got = finish_follow.finish(rows)["ann"]
    assert got["shown"] and got["n"] == 9 and got["passed"] == 7 and got["rate"] == pytest.approx(0.778, abs=1e-3)
    assert got["by_agent"] == 5 and got["by_bridge"] == 2
    assert got["missed"] == {"clock": 1, "chronicle": 1} and got["last"] == "2026-11-12"
    assert "bridge" in got["bridge_note"] and "setup finishing" in got["bridge_note"]
    assert "never ranked" in got["note"]


def test_finish_is_hidden_below_the_threshold_and_empty_is_n_zero():
    rows = [_ob("bob", True)] * (finish_follow.MIN_FINISH - 1)
    got = finish_follow.finish(rows)["bob"]
    assert got["shown"] is False and got["n"] == finish_follow.MIN_FINISH - 1 and "rate" not in got and got["passed"] is None
    empty = finish_follow.finish_row({}, "nobody")
    assert empty["n"] == 0 and empty["shown"] is False
    assert finish_follow.finish([*rows, _ob("bob", False)])["bob"]["rate"] == pytest.approx(0.875)


def test_the_bridge_marker_is_read_through_one_function():
    assert finish_follow.done_by_bridge({"by": "bridge", "x": 1}) is True
    assert not finish_follow.done_by_bridge({"by": "agent"}) and not finish_follow.done_by_bridge({}) and not finish_follow.done_by_bridge(None)
    assert (finish_follow.BRIDGE_KEY, finish_follow.BRIDGE_VALUE) == ("by", "bridge")


def _obl(agent="ann", **kw):
    base = dict(module="emporia:tesseract:v1", status="completed", participants=["ann", "zed"], outcome={},
                quitter=None, actions=[], at="2026-11-10T00:00:00+00:00")
    base.update(kw)
    return finish_follow.session_obligation(agent, **base)


def test_session_obligations_from_fixture_sessions():
    sealed_by_bridge = [("seal", {}), ("open", {"by": "bridge"}), ("reveal", {"by": "bridge"})]
    got = _obl(actions=sealed_by_bridge, outcome={"termination": "score", "unrevealed": []})
    assert got["ok"] and got["kind"] == "reveal" and got["by"] == "bridge"           # sealed and opened, by the bridge
    mixed = _obl(actions=[("open", {}), ("reveal", {"by": "bridge"})], outcome={})
    assert mixed["ok"] and mixed["by"] == "agent"                                     # the agent did some itself
    assert _obl(outcome={"termination": "unrevealed"})["kind"] == "reveal"          # nobody revealed
    missed = _obl(outcome={"unrevealed": ["ann"]})
    assert missed["ok"] is False and missed["kind"] == "reveal"
    assert _obl(outcome={"unrevealed": ["zed"]})["ok"] is True                        # the other one's failure is not ann's
    assert _obl(outcome={"termination": "forfeit", "refused_by": "ann"})["ok"] is False
    late = _obl(outcome={"termination": "timeout", "out": ["ann"], "winner": None})
    assert late["ok"] is False and late["kind"] == "clock"
    assert _obl(module="emporia:blotto:v2", outcome={"termination": "timeout", "winner": "zed"})["kind"] == "clock"
    assert _obl(module="emporia:blotto:v2", outcome={"termination": "timeout", "winner": "ann"})["ok"] is True
    assert _obl(outcome={"termination": "void"}) is None
    gone = _obl(status="abandoned", quitter="ann")
    assert gone["ok"] is False and gone["kind"] == "game"
    assert _obl(status="abandoned", quitter="zed") is None                           # someone else left
    assert _obl("cat") is None                                                        # not a player
    svc = {"module": "emporia:service:v1"}
    assert _obl(**svc, actions=[("accept", {})], outcome={"status": "timeout_no_delivery"})["ok"] is False
    assert _obl(**svc, actions=[("accept", {}), ("deliver", {})], outcome={"status": "confirmed"})["kind"] == "service"
    assert _obl(**svc, actions=[], outcome={"status": "timeout_no_delivery"}) is None  # never accepted: nothing owed


def test_finish_reads_the_sheet_from_sessions_and_never_reaches_boards_or_allowance(w, monkeypatch):
    import inspect

    from emporia import allowance
    from emporia.routes import standing as st
    ann, zed = w.register("ann", "o1"), w.register("zed", "o2")
    mod = next(iter(st.game_module_types()))
    plan = ["done"] * 6 + ["late", "gone", "open", "open2"]
    sessions, outcomes = [], {}
    for i, what in enumerate(plan):
        sid = f"fin{w.tag}{i}"
        outcomes[sid] = ({"termination": "timeout", "out": [ann], "winner": zed} if what == "late"
                         else {"termination": "score"})
        sessions.append({"session_id": sid, "module_type": mod, "participants": [ann, zed],
                         "status": "abandoned" if what == "gone" else "completed", "step_number": 4,
                         "updated_at": f"2026-11-{10 + i:02d}T00:00:00+00:00", "config": {},
                         "state": {"sid": sid}, "current_agent": ann})
        if what.startswith("open"):
            with rs.DB_LOCK, rs.get_db() as conn:
                conn.execute("INSERT INTO actions (action_id, session_id, agent_id, action_type, payload_json, "
                             "created_at) VALUES (?,?,?,?,?,?)",
                             (f"{sid}a", sid, ann, "reveal", json.dumps({"by": "bridge"} if what == "open2" else {}),
                              "x"))
                conn.commit()

    class Module:
        def is_terminal(self, state):
            return True, outcomes[state["sid"]]

    monkeypatch.setattr(st, "get_interaction_module", lambda _t: Module())
    monkeypatch.setattr(st.SessionState, "from_dict", classmethod(lambda cls, d: d))
    monkeypatch.setattr(rs, "_session_visibility", lambda s: "public")
    row = st._finish_and_setup(ann, sessions, frozenset())[0]
    assert row["n"] == 10 and row["passed"] == 8 and row["shown"] and row["by_bridge"] == 1 and row["by_agent"] == 7
    assert row["missed"] == {"clock": 1, "game": 1}
    assert st._finish_and_setup(ann, sessions, frozenset({zed}))[0]["n"] == 0                  # house games count nothing
    # never on a board, never in allowance
    src = inspect.getsource(allowance)
    assert '"finish"' not in src and "'finish'" not in src and "finish_follow.finish" not in src


def test_an_old_sheet_without_finish_still_renders():
    text = progression.render({"agent_id": "ann", "craft": {}})
    assert "Finish" not in text
    shown = finish_follow.finish([_ob("ann", True)] * 9 + [_ob("ann", True, "reveal", "bridge")])["ann"]
    out = "\n".join(progression._render_finish(shown, None, None))
    assert "10 of 10 obligations completed" in out and "1 by its bridge" in out and "setup finishing" in out
    assert "so far" in "\n".join(progression._render_finish(finish_follow.finish([_ob("ann", True)])["ann"], None, None))
    assert progression._render_finish(None, None, None) == []


def test_the_sheet_carries_finish_additively_and_hidden_for_a_new_agent(w):
    ann = w.register("ann", "o1")
    sheet = w.client.get(f"/agents/{ann}/sheet").json()
    assert sheet["finish"]["n"] == 0 and sheet["finish"]["shown"] is False and "never ranked" in sheet["finish"]["note"]
    assert sheet["follow"] == {"n": 0}
    assert "Finish" not in progression.render(sheet) and "Follow" not in progression.render(sheet)   # nothing yet


def test_the_sheet_renders_finish_and_follow_lines():
    text = progression._render_finish(None, {"n": 4, "passed": 3, "rate": 0.75, "note": "never ranked"}, None)
    assert text == ["Follow   3 of 4 Chronicle passages kept their card (never ranked)"]


def test_finish_reads_the_bridge_mark_from_the_signed_rationale_in_the_audit_log(monkeypatch):
    """The bridge marks an open by its signed rationale (`[bridge] ...`), which the relay keeps in the session's
    public audit log, not in `actions`: Finish joins it in and counts the step as the bridge's."""
    from emporia.routes import standing
    from emporia.tesseract_client import BRIDGE_MARK
    log = [{"sender": "a", "action": "seal", "rationale": "sealing"},
           {"sender": "a", "action": "open", "rationale": BRIDGE_MARK + "opening my sealed push of round 1"},
           {"sender": "b", "action": "open", "rationale": "mine"}]
    monkeypatch.setattr("emporia.session_audit.get_public_log", lambda sid: log)
    acts = {"s1": [("seal", {"commitment": "c"}), ("open", {"move": [1], "nonce": "n"})]}
    standing._join_bridge_marks("a", acts)
    assert finish_follow.done_by_bridge(acts["s1"][1][1])
    assert not finish_follow.done_by_bridge({"move": [1], "rationale": "opened by hand"})
    assert finish_follow.done_by_bridge({"by": "bridge"})


def test_a_chronicle_seal_opened_is_a_kept_finish_obligation_and_an_unopened_one_is_missed(monkeypatch):
    from emporia.routes import chronicle as ch
    from emporia.routes import standing as st

    def slot(mode="live", opened=(), balloted=()):
        return {"called": True, "mode": mode, "call_ts": "2026-11-10T00:00:00+00:00", "windows": {"count": [0, 10]},
                "inputs": {"seals": [{"agent": "ann", "what": "passage", "at": "2026-11-10T01:00:00+00:00"},
                                     {"agent": "ann", "what": "pick"}, {"agent": "bob", "what": "passage"}],
                           "reveals": [{"agent": a} for a in opened], "ballots": [{"agent": a} for a in balloted]}}
    slots = [slot(opened=["ann"]), slot(mode="practice"), {**slot(), "called": False},
             {**slot(), "windows": {"count": [0, 10**12]}}]
    monkeypatch.setattr(relay_modules, "off_because", lambda *a: "")
    monkeypatch.setattr(ch, "sequence", lambda: ({}, slots, 100.0))
    got = st._chronicle_obligations("ann")
    assert [(o["kind"], o["ok"], o["by"]) for o in got] == [("chronicle", True, "agent"), ("chronicle", False, "agent")]
    assert finish_follow.finish(got)["ann"]["n"] == 2
    assert st._chronicle_obligations("bob")[0]["ok"] is False
    monkeypatch.setattr(relay_modules, "off_because", lambda *a: "off")
    assert st._chronicle_obligations("ann") == []
