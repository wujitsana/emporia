"""Pledges: a signed, public promise to a counterparty, resolved by the record or attested.

The fixed list of kinds, each resolution path (evidence: delivery and response times, reveal
turns, declared moves; attestation and contest), the anti-farming rules (self, own operator,
the house, a real counterparty in a real session, one attestation, swaps), the Trust knob,
the playbook bound, and what the relay shows to whom."""
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import relay_server as rs
from emporia import identity, pledges as PL, playbook as P, progression, quests as Q, signing, wire
from emporia import relay_server as rs
from tests.test_emporia import _register_keyed

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def ts(seconds: float) -> float:
    return (T0 + timedelta(seconds=seconds)).timestamp()


def pledge(kind, terms=None, made=0, maker="m", to="c", subject_type="session"):
    return {"pledge_id": f"plg_{uuid.uuid4().hex[:8]}", "maker_id": maker, "to_agent": to, "kind": kind,
            "subject_type": subject_type, "subject_id": "sess_x", "terms": terms or {}, "made_at": at(made)}


def deal(status="active", deal_status="accepted", delivered=None, updated=0, seller="m", buyer="c"):
    return {"status": status, "participants": [buyer, seller], "updated_at": at(updated), "turn_order": True,
            "state": {"data": {"buyer": buyer, "seller": seller, "status": deal_status,
                               "delivered_at": at(delivered) if delivered is not None else None}}}


def act(agent, kind, t, move=None):
    return {"agent_id": agent, "action_type": kind, "payload": {"move": move} if move is not None else {},
            "created_at": at(t)}


# ── the payloads ────────────────────────────────────────────────────────────

def test_the_pledge_and_its_acts_are_their_own_signed_kinds():
    p = wire.signed_pledge_payload("m", "c", "tell", "session", "sess_x", {"tell": "even_split"}, "", "n" * 16, "t")
    assert p["type"] == "emporia:pledge:v2" and p["terms"] == {"tell": "even_split"}
    a = wire.signed_pledge_act_payload("plg_1", "attest", "kept", "c", "t")
    assert a["type"] == "emporia:pledge-act:v2"
    assert signing.KINDS["pledge"].builder == "signed_pledge_payload"
    assert signing.KINDS["pledge-act"].builder == "signed_pledge_act_payload"
    assert "pledge" in signing.DEFAULT_LIMITS
    assert PL.pledge_id_for("m", "abc") == PL.pledge_id_for("m", "abc") != PL.pledge_id_for("x", "abc")


def test_a_pledge_signature_verifies_and_a_changed_term_does_not():
    agent = f"pl_sig_{uuid.uuid4().hex[:6]}"
    args = (agent, "c", "deliver_by", "session", "sess_x", {"by": at(3600)}, "on time", "n" * 16, at(0))
    sig = signing.sign(agent, "pledge", *args)
    pub = identity.get_public_key_hex(agent)
    assert identity.verify(wire.signed_pledge_payload(*args), sig, pub)
    changed = (*args[:5], {"by": at(7200)}, *args[6:])
    assert not identity.verify(wire.signed_pledge_payload(*changed), sig, pub)


def test_terms_come_from_the_fixed_list():
    now = ts(0)
    assert PL.check_terms("deliver_by", {"by": at(3600)}, "emporia:service:v1", now) == ""
    assert PL.check_terms("deliver_by", {"by": at(-1)}, "emporia:service:v1", now), "a deadline already past"
    assert PL.check_terms("deliver_by", {"by": at(40 * 86400)}, "emporia:service:v1", now), "too far ahead"
    assert PL.check_terms("deliver_by", {"by": at(60)}, "emporia:blotto:v1", now), "a deal pledge on a game"
    assert PL.check_terms("tell", {"tell": "even_split"}, "emporia:blotto:v1", now) == ""
    assert PL.check_terms("tell", {"tell": "even_split"}, "emporia:two-thirds:v1", now), "blotto only"
    assert PL.check_terms("tell", {"tell": "pick_below", "value": 30}, "emporia:two-thirds:v1", now) == ""
    assert PL.check_terms("tell", {"tell": "pick_below"}, "emporia:two-thirds:v1", now), "needs a number"
    assert PL.check_terms("tell", {"tell": "my best"}, "emporia:blotto:v1", now), "nothing vague counts"
    assert PL.check_terms("reveal_within", {"seconds": 120}, "emporia:chess:v1", now), "commit-reveal games only"
    assert PL.check_terms("reveal_within", {"seconds": 1}, "emporia:blotto:v1", now)
    assert PL.check_terms("refund", {"anything": 1}, "emporia:service:v1", now)
    assert PL.check_terms("vibes", {}, "", now)
    assert PL.check_note("x" * 201) and PL.check_note("two\nlines") and PL.check_note("fine") == ""


# ── resolution: evidence ────────────────────────────────────────────────────

def test_deliver_by_is_kept_or_broken_by_the_delivery_time():
    p = pledge("deliver_by", {"by": at(3600)})
    kept = PL.resolve(p, deal(deal_status="delivered", delivered=1800), [], {}, ts(2000))
    assert (kept["status"], kept["resolved_by"]) == ("kept", "evidence")
    late = PL.resolve(p, deal(deal_status="delivered", delivered=4000), [], {}, ts(5000))
    assert late["status"] == "broken" and late["evidence"]["delivered_at"] == at(4000)
    assert PL.resolve(p, deal(), [], {}, ts(3000))["status"] == "open"
    due = PL.resolve(p, deal(), [], {}, ts(3000))
    assert (due["due_at"], due["do"]) == (at(3600), "deliver")
    assert PL.resolve(p, deal(), [], {}, ts(3700))["status"] == "broken", "not delivered by the time"
    called_off = PL.resolve(p, deal(status="abandoned", updated=100), [], {}, ts(4000))
    assert called_off["status"] == "void", "a deal called off tests nothing"


def test_respond_within_is_measured_from_the_delivery():
    p = pledge("respond_within", {"seconds": 600}, maker="c", to="m")
    d = deal(deal_status="delivered", delivered=100)
    assert PL.resolve(p, d, [act("c", "confirm", 500)], {}, ts(900))["status"] == "kept"
    assert PL.resolve(p, d, [act("c", "dispute", 800)], {}, ts(900))["status"] == "broken"
    assert PL.resolve(p, d, [], {}, ts(800))["status"] == "broken", "no answer in time"
    assert PL.resolve(p, d, [], {}, ts(300))["do"] == "respond"
    assert PL.resolve(p, deal(status="completed", updated=50), [], {}, ts(900))["status"] == "void"


def pick(status="completed", phase="reveal", pending=(), updated=1000):
    return {"status": status, "participants": ["m", "c", "x"], "updated_at": at(updated), "turn_order": False,
            "current_agent": "m", "state": {"data": {"phase": phase, "pending": list(pending)}}}


def test_reveal_within_counts_only_turns_that_opened_after_the_pledge():
    p = pledge("reveal_within", {"seconds": 120}, made=5)
    commits = [act("m", "commit", 10), act("c", "commit", 20), act("x", "commit", 30)]
    on_time = PL.resolve(p, pick(pending=[]), [*commits, act("c", "reveal", 40), act("m", "reveal", 100),
                                               act("x", "reveal", 110)], {}, ts(2000))
    assert (on_time["status"], on_time["resolved_by"]) == ("kept", "evidence")
    late = PL.resolve(p, pick(pending=[]), [*commits, act("m", "reveal", 200), act("c", "reveal", 210),
                                            act("x", "reveal", 220)], {}, ts(2000))
    assert late["status"] == "broken" and late["evidence"]["turn_opened_at"] == at(30)
    waiting = PL.resolve(p, pick(status="active", pending=["m"]), commits, {}, ts(60))
    assert (waiting["status"], waiting["do"], waiting["due_at"]) == ("open", "reveal", at(150))
    gone = PL.resolve(p, pick(status="active", pending=["m"]), commits, {}, ts(400))
    assert gone["status"] == "broken", "its reveal turn ran past the pledge"
    timed_out = PL.resolve(p, pick(pending=["m"], updated=700), commits, {}, ts(800))
    assert timed_out["status"] == "broken", "a game that ended on its unrevealed turn"
    after = pledge("reveal_within", {"seconds": 120}, made=35)
    assert PL.resolve(after, pick(pending=[]), [*commits, act("m", "reveal", 300)], {}, ts(2000))["status"] == "void", \
        "a pledge made once the turn was open says nothing"


def test_blotto_reveal_turns_open_at_the_move_before():
    p = pledge("reveal_within", {"seconds": 60})
    blotto = {"status": "completed", "participants": ["c", "m"], "updated_at": at(500), "turn_order": True,
              "current_agent": "c", "state": {"data": {"phase": "commit"}}}
    acts = [act("c", "commit", 10), act("m", "commit", 20), act("c", "reveal", 200), act("m", "reveal", 230)]
    assert PL.resolve(p, blotto, acts, {}, ts(600))["status"] == "kept", "measured from c's reveal, not the commit"


def test_a_tell_is_held_against_the_revealed_move():
    p = pledge("tell", {"tell": "even_split"}, made=1)
    game = {"status": "completed", "participants": ["c", "m"], "updated_at": at(500), "turn_order": True,
            "state": {"data": {"phase": "commit"}}}
    kept = PL.resolve(p, game, [act("c", "commit", 10), act("m", "commit", 20), act("c", "reveal", 30, [5, 5, 0]),
                                act("m", "reveal", 40, [4, 3, 3])], {}, ts(600))
    assert kept["status"] == "kept" and kept["evidence"]["revealed"] == [[4, 3, 3]]
    broken = PL.resolve(p, game, [act("m", "commit", 20), act("m", "reveal", 40, [10, 0, 0])], {}, ts(600))
    assert broken["status"] == "broken"
    before = pledge("tell", {"tell": "even_split"}, made=25)
    assert PL.resolve(before, game, [act("m", "commit", 20), act("m", "reveal", 40, [4, 3, 3])], {},
                      ts(600))["status"] == "void", "a move committed before the pledge is not a tell"
    below = pledge("tell", {"tell": "pick_below", "value": 30})
    assert PL.tell_holds(below["terms"], 29) and not PL.tell_holds(below["terms"], 30)
    assert PL.tell_holds({"tell": "move", "value": [1, 2]}, [1, 2])


def test_a_listing_pledge_waits_for_a_deal_with_the_counterparty():
    p = pledge("deliver_by", {"by": at(3600)}, subject_type="listing")
    assert PL.resolve(p, {"listing_expires_at": at(7200)}, [], {}, ts(100))["waiting_on"] == \
        "a deal with the counterparty"
    assert PL.resolve(p, {"listing_expires_at": at(7200)}, [], {}, ts(4000))["status"] == "void"
    assert PL.resolve(p, deal(deal_status="delivered", delivered=1000), [], {}, ts(1200))["status"] == "kept"


# ── resolution: attestation ─────────────────────────────────────────────────

def test_a_refund_pledge_is_attested_by_the_buyer_and_may_be_contested():
    p = pledge("refund")
    ended = deal(status="completed", deal_status="confirmed", updated=100)
    assert PL.resolve(p, deal(), [], {}, ts(50))["waiting_on"].startswith("the deal's end")
    waiting = PL.resolve(p, ended, [], {}, ts(200))
    assert waiting["waiting_on"] == "counterparty" and waiting["attest_by"]
    assert PL.resolve(p, ended, [], {"attest": {"verdict": "kept", "at": at(300)}}, ts(400))["status"] == "kept"
    broken = PL.resolve(p, ended, [], {"attest": {"verdict": "broken", "at": at(300)}}, ts(400))
    assert broken["status"] == "broken" and broken["contest_by"]
    disputed = PL.resolve(p, ended, [], {"attest": {"verdict": "broken", "at": at(300)}, "contest": {"at": at(500)}},
                          ts(600))
    assert (disputed["status"], disputed["resolved_by"]) == ("disputed", "attestation")
    assert PL.resolve(p, ended, [], {}, ts(100 + 8 * 86400))["status"] == "void", "not attested in time"


def test_a_session_that_never_started_tests_nothing():
    p = pledge("tell", {"tell": "even_split"})
    waiting = {"status": "waiting", "participants": ["m"], "updated_at": at(0), "state": {"data": {}}}
    assert PL.resolve(p, waiting, [], {}, ts(10))["waiting_on"] == "the session to start"
    assert PL.resolve(p, {**waiting, "status": "abandoned"}, [], {}, ts(10))["status"] == "void"


# ── the record and anti-farming ─────────────────────────────────────────────

OPS = {"m": "op1", "m2": "op1", "c": "op2", "c2": "op3", "h": "op9"}


def resolved(maker, to, status, by="evidence", t=100, **extra):
    return {"pledge_id": f"plg_{uuid.uuid4().hex[:8]}", "maker_id": maker, "to_agent": to, "kind": "deliver_by",
            "subject_type": "session", "subject_id": "sess_y", "status": status, "resolved_by": by,
            "resolved_at": at(t), "made_at": at(0), **extra}


def test_own_operator_and_house_pledges_count_nothing_and_say_why():
    rows = [resolved("m", "c", "kept"), resolved("m", "c2", "broken"), resolved("m", "m2", "kept"),
            resolved("m", "h", "kept"), resolved("m", "c", "kept", status_note=1), resolved("m", "c", "void")]
    rec = PL.record("m", rows, operator=OPS.get, house=frozenset({"h"}), now=ts(200))
    assert (rec["kept"], rec["broken"], rec["disputed"], rec["not_counted"], rec["void"]) == (2, 1, 0, 2, 1)
    assert any("own operator" in w for w in rec["why"]) and any("house" in w for w in rec["why"])
    assert rec["last_30_days"]["kept"] == 2 and rec["kept_with_evidence"] == 2
    assert "m2" not in json.dumps(rec), "the sheet never says which agents share an operator"


def test_a_private_deals_pledge_is_counted_without_its_counterparty():
    rows = [resolved("m", "c", "kept", public=False), resolved("m", "c2", "kept", public=True)]
    rec = PL.record("m", rows, operator=OPS.get, now=ts(200), public=lambda p: p["public"])
    private = [r for r in rec["recent"] if r.get("private")]
    assert len(private) == 1 and "to_agent" not in private[0] and "subject_id" not in private[0]
    assert rec["kept"] == 2


def test_trust_points_flatten_repeat_counterparties_and_cost_more_when_broken():
    kept = [resolved("m", "c", "kept", t=i) for i in range(1, 4)]
    pts, _ = PL.trust_points("m", kept, operator=OPS.get)
    assert pts == pytest.approx(0.5 * (1 + 1 / 2 + 1 / 3), abs=1e-3), "the nth with one operator weighs 1/n"
    other = [resolved("m", "c", "kept"), resolved("m", "c2", "kept")]
    assert PL.trust_points("m", other, operator=OPS.get)[0] == pytest.approx(1.0)
    one_broken = [resolved("m", "c", "kept"), resolved("m", "c2", "broken")]
    assert PL.trust_points("m", one_broken, operator=OPS.get)[0] == pytest.approx(-0.5), "broken costs twice"
    own = [resolved("m", "m2", "kept"), resolved("m", "h", "kept")]
    assert PL.trust_points("m", own, operator=OPS.get, house=frozenset({"h"}))[0] == 0
    weak, why = PL.trust_points("m", [resolved("m", "c", "kept")], operator=OPS.get, weight=lambda a, t: 0.25)
    assert weak == pytest.approx(0.125) and any("stands for" in w for w in why)


def test_swapped_attestations_weigh_nothing():
    mine = resolved("m", "c", "kept", by="attestation")
    theirs = resolved("c", "m", "kept", by="attestation")
    pts, why = PL.trust_points("m", [mine], operator=OPS.get, all_pledges=[mine, theirs])
    assert pts == 0 and any("swap" in w for w in why)
    assert PL.trust_points("m", [mine], operator=OPS.get, all_pledges=[mine])[0] == pytest.approx(0.25)


def test_the_trust_knob_is_off_by_default(monkeypatch):
    monkeypatch.delenv("EMPORIA_PLEDGES_TRUST", raising=False)
    assert not PL.trust_on()
    monkeypatch.setenv("EMPORIA_PLEDGES_TRUST", "on")
    assert PL.trust_on()
    word = {"trust_points": 3.0}
    base = dict(table={}, deals=[], abandoned=[], registered_at=None, now=ts(0))
    off = progression.base_sheet("m", **base, word={"line": "x"})
    on = progression.base_sheet("m", **base, word=word)
    assert off["trust"]["score"] + 3.0 == on["trust"]["score"] and on["formula"].endswith(PL.TRUST_FORMULA)
    assert off["formula"] == progression.SHEET_FORMULA and "pledges" in off


def test_keep_your_word_needs_evidence_and_a_real_counterparty():
    rows = [resolved("m", "c", "kept", by="attestation", t=10), resolved("m", "m2", "kept", t=20),
            resolved("m", "c", "kept", t=30)]
    assert PL.first_kept_with_evidence("m", rows, operator=OPS.get) == at(30)
    out = Q.progress("m", posts=[], games=[], deals=[], reports=[], pledges_on=True, kept_word_at=at(30))
    kw = next(q for q in out["quests"] if q["id"] == "keep-word")
    assert kw["done"] and kw["optional"] and out["xp"] == 15 and out["next"] != kw.get("how")
    hidden = Q.progress("m", posts=[], games=[], deals=[], reports=[])
    assert "keep-word" not in {q["id"] for q in hidden["quests"]}, "only where the relay runs pledges"


def test_the_due_list_says_what_each_pledge_asks_soonest_first():
    rows = [{**pledge("deliver_by"), "status": "open", "do": "deliver", "due_at": at(900), "subject_id": "s1"},
            {**pledge("reveal_within"), "status": "open", "do": "reveal", "due_at": at(100), "subject_id": "s2"},
            {**pledge("refund", maker="x", to="m"), "status": "open", "waiting_on": "counterparty",
             "attest_by": at(5000)},
            {**pledge("tell"), "status": "kept"}]
    due = PL.due("m", rows, ts(0))
    assert [d["do"] for d in due] == ["reveal", "deliver", "attest"]
    assert due[0]["seconds_left"] == 100 and due[0]["session_id"] == "s2"


# ── the playbook bound ──────────────────────────────────────────────────────

def test_the_playbook_bounds_pledges_yes_for_custom_no_for_the_house():
    assert P.PRESETS["custom"].pledges and not P.PRESETS["host"].pledges
    assert P.resolve({"EMPORIA_PLAYBOOK": "custom", "EMPORIA_PLAYBOOK_PLEDGES": "no"}).pledges is False
    assert P.resolve({"EMPORIA_PLAYBOOK": "host", "EMPORIA_PLAYBOOK_PLEDGES": "yes"}).pledges is True
    with pytest.raises(P.PlaybookError):
        P.resolve({"EMPORIA_PLAYBOOK_PLEDGES": "sometimes"})
    assert "makes no pledges" in P.may_pledge(P.PRESETS["host"]) and P.may_pledge(P.PRESETS["custom"]) == ""
    assert "pledges" in P.about(P.resolve({"EMPORIA_PLAYBOOK": "player", "EMPORIA_PLAYBOOK_PLEDGES": "no"}))


def test_the_mcp_tools_refuse_pledges_the_playbook_rules_out(monkeypatch):
    from emporia import mcp_server as m
    monkeypatch.setenv("EMPORIA_PLAYBOOK", "host")
    monkeypatch.setattr(m, "AGENT_ID", "pl_house")
    called = []
    monkeypatch.setattr(m, "_bearer_request", lambda *a, **k: called.append(a))
    for out in (m.make_pledge("tell", "sess_x", {"tell": "even_split"}, to_agent="c"),
                m.attest_pledge("plg_x", "kept"), m.attest_pledge("plg_x", "contest")):
        assert not out["success"] and "makes no pledges" in out["error"] and out["playbook"] == "host"
    assert not called, "refused before anything is signed or sent"


# ── the relay ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests,pledges")
    monkeypatch.delenv("EMPORIA_HOUSE_AGENTS", raising=False)
    monkeypatch.delenv("EMPORIA_PLEDGES_TRUST", raising=False)
    return TestClient(rs.app, client=("127.0.0.1", 51298))


def as_(agent):
    return {"x-emporia-agent-id": agent}


def agents(client, *tags):
    out = [f"pl_{t}_{uuid.uuid4().hex[:6]}" for t in tags]
    for a in out:
        _register_keyed(client, a)
    return out


def same_operator(*names):
    with rs.DB_LOCK, rs.get_db() as conn:
        nous = f"nous_{uuid.uuid4().hex[:6]}"
        for n in names:
            conn.execute("UPDATE authorized_agents SET nous_user_id = ? WHERE agent_id = ?", (nous, n))


def session(module, players, data, status="active", current=""):
    sid = f"sess_{uuid.uuid4().hex[:12]}"
    state = {"data": data, "current_agent": current or players[0], "step_number": 1,
             "metadata": {"participants": players}}
    now = datetime.now(UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        for p in players:
            conn.execute("INSERT INTO participants (session_id, agent_id, joined_at) VALUES (?, ?, ?)", (sid, p, now))
        conn.execute("INSERT INTO sessions (session_id, module_type, config_json, payment_rules_json, state_json, "
                     "current_agent, step_number, status, participants_json, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (sid, module, "{}", "{}", json.dumps(state), current or players[0],
                                                        1, status, json.dumps(players), now, now))
    _MADE.append(sid)
    return sid


_MADE: list[str] = []


@pytest.fixture(autouse=True)
def _drop_made_sessions():
    """These sessions carry only the state a pledge reads, not a full game's. Left active in
    the suite's shared database, the deadline sweeper (which reads every timed session)
    would stop on one and never reach a later test's game; so each test takes its own away."""
    yield
    made, _MADE[:] = list(_MADE), []
    if made:
        marks = ",".join("?" * len(made))
        with rs.DB_LOCK, rs.get_db() as conn:
            for table in ("actions", "participants", "sessions"):
                conn.execute(f"DELETE FROM {table} WHERE session_id IN ({marks})", made)  # noqa: S608


def set_session(sid, status, data, current=""):
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT state_json FROM sessions WHERE session_id = ?", (sid,)).fetchone()
        state = json.loads(row[0])
        state["data"] = data
        conn.execute("UPDATE sessions SET status = ?, state_json = ?, current_agent = ?, updated_at = ? "
                     "WHERE session_id = ?", (status, json.dumps(state), current or state["current_agent"],
                                              datetime.now(UTC).isoformat(), sid))


def record_action(sid, agent, kind, move=None, when=None):
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO actions (action_id, session_id, agent_id, action_type, payload_json, created_at) "
                     "VALUES (?,?,?,?,?,?)", (f"act_{uuid.uuid4().hex[:10]}", sid, agent, kind,
                                              json.dumps({"move": move} if move is not None else {}),
                                              (when or datetime.now(UTC)).isoformat()))


def _sid(agent):
    return rs.AGENT_REGISTRY.get_stable_id(agent) or ""


def make(client, maker, to, kind, subject_id, terms=None, subject_type="session", sign_as=None, note=""):
    nonce, when = uuid.uuid4().hex, datetime.now(UTC).isoformat()
    terms = terms or {}
    sig = signing.sign(sign_as or maker, "pledge", _sid(maker), _sid(to), kind, subject_type, subject_id, terms, note, nonce, when)
    return client.post("/pledges", headers=as_(maker), json={
        "kind": kind, "subject_type": subject_type, "subject_id": subject_id, "to_agent": to, "terms": terms,
        "note": note, "nonce": nonce, "at": when, "signature": sig})


def attest(client, agent, pid, act_, verdict=""):
    when = datetime.now(UTC).isoformat()
    sig = signing.sign(agent, "pledge-act", pid, act_, verdict, _sid(agent), when)
    return client.post(f"/pledges/{pid}/acts", headers=as_(agent),
                       json={"act": act_, "verdict": verdict, "at": when, "signature": sig})


def test_off_unless_the_relay_runs_pledges(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    assert client.get("/pledges").status_code == 404
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests,pledges")
    card = client.get("/relay/card").json()["relay_modules"]["gamify"]
    assert "pledges" in card["parts"] and PL.FORMULA in card["formulas"] and card["pledges_in_trust"] is False


def test_a_tell_on_a_public_game_is_signed_checked_and_shown(client):
    m, c = agents(client, "m", "c")
    sid = session("emporia:blotto:v1", [c, m], {"phase": "commit", "commitments": {}, "revealed": {}})
    assert make(client, m, m, "tell", sid, {"tell": "even_split"}).status_code == 400, "a pledge to yourself"
    assert make(client, m, c, "tell", sid, {"tell": "even_split"}, sign_as=c).status_code == 403, "not its signature"
    assert make(client, m, c, "tell", sid, {"tell": "all in"}).status_code == 422
    r = make(client, m, c, "tell", sid, {"tell": "even_split"}, note="I play even")
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["status"] == "open" and p["public"] and p["signature"] and p["note"] == "I play even"
    record_action(sid, c, "commit")
    record_action(sid, m, "commit")
    record_action(sid, c, "reveal", [5, 5, 0])
    record_action(sid, m, "reveal", [4, 3, 3])
    set_session(sid, "completed", {"phase": "commit", "history": [{}]})
    listed = client.get("/pledges", params={"maker": m}).json()["pledges"]
    assert [(x["pledge_id"], x["status"], x["resolved_by"]) for x in listed] == [(p["pledge_id"], "kept", "evidence")]
    sheet = client.get(f"/agents/{m}/sheet").json()
    assert sheet["pledges"]["kept"] == 1 and sheet["pledges"]["in_trust"] is False
    assert sheet["formula"] == progression.SHEET_FORMULA, "season-0 shows the record only"
    quests = client.get(f"/agents/{m}/quests").json()
    assert next(q for q in quests["quests"] if q["id"] == "keep-word")["done"]
    assert "due" not in quests, "what is due is the agent's own, not the public's"
    assert attest(client, c, p["pledge_id"], "attest", "broken").status_code == 409, "the record decides a tell"


def test_pledges_take_the_live_v2_set_plays_and_not_bargain(client):
    """gamify-all (found by the end-to-end run): pledges knew only the :v1 set plays, which
    calls retired, so no tell or reveal pledge could be made on a live game. A :v2 tell is
    kept by its revealed move, with a call beside it; Bargain is no commit-reveal game."""
    now = datetime.now(UTC).timestamp()
    assert PL.check_terms("tell", {"tell": "even_split"}, "emporia:blotto:v2", now) == ""
    assert PL.check_terms("tell", {"tell": "pick_above", "value": 3}, "emporia:lowest-unique:v2", now) == ""
    assert PL.check_terms("reveal_within", {"seconds": 60}, "emporia:two-thirds:v2", now) == ""
    assert "commit-reveal" in PL.check_terms("tell", {"tell": "move", "value": 1}, "emporia:bargain:v2", now)
    m, c = agents(client, "m", "c")
    sid = session("emporia:blotto:v2", [c, m], {"phase": "commit", "commitments": {}, "revealed": {}})
    r = make(client, m, c, "tell", sid, {"tell": "move", "value": [4, 3, 3]})
    assert r.status_code == 200, r.text
    record_action(sid, c, "commit")
    record_action(sid, m, "commit")
    record_action(sid, c, "reveal", [5, 5, 0])
    record_action(sid, m, "reveal", [4, 3, 3])
    set_session(sid, "completed", {"phase": "commit", "history": [{}], "calls": {m: 60}})
    listed = client.get("/pledges", params={"maker": m}).json()["pledges"]
    assert [(x["status"], x["resolved_by"]) for x in listed] == [("kept", "evidence")]


def test_who_may_pledge_on_what(client):
    m, c, x = agents(client, "m", "c", "x")
    sid = session("emporia:blotto:v1", [c, m], {"phase": "commit"})
    assert make(client, x, c, "tell", sid, {"tell": "even_split"}).status_code == 403, "not in the game"
    assert make(client, m, x, "tell", sid, {"tell": "even_split"}).status_code == 400, "not a counterparty in it"
    done = session("emporia:blotto:v1", [c, m], {"phase": "commit"}, status="completed")
    assert make(client, m, c, "tell", done, {"tell": "even_split"}).status_code == 409, "made before it is known"
    deal_id = session("emporia:service:v1", [c, m], {"buyer": c, "seller": m, "status": "accepted",
                                                     "deadline": (datetime.now(UTC) + timedelta(days=1)).isoformat()})
    by = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    assert make(client, c, m, "deliver_by", deal_id, {"by": by}).status_code == 403, "the seller's pledge"
    assert make(client, m, c, "tell", deal_id, {"tell": "even_split"}).status_code == 422
    assert make(client, m, c, "deliver_by", deal_id, {"by": by}).status_code == 200


def test_a_private_deals_pledge_is_shown_to_its_two_agents_only(client):
    m, c, x = agents(client, "m", "c", "x")
    deal_id = session("emporia:service:v1", [c, m], {"buyer": c, "seller": m, "status": "accepted",
                                                     "deadline": (datetime.now(UTC) + timedelta(days=1)).isoformat()})
    by = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    p = make(client, m, c, "deliver_by", deal_id, {"by": by}).json()
    assert p["public"] is False
    assert client.get(f"/pledges/{p['pledge_id']}", headers=as_(x)).status_code == 404
    assert client.get(f"/pledges/{p['pledge_id']}").status_code == 404
    assert client.get(f"/pledges/{p['pledge_id']}", headers=as_(c)).status_code == 200
    assert not client.get("/pledges", params={"maker": m}, headers=as_(x)).json()["pledges"]
    assert client.get("/pledges", params={"maker": m}, headers=as_(m)).json()["pledges"]
    due = client.get("/pledges", params={"due": "true"}, headers=as_(m)).json()["due"]
    assert [(d["do"], d["session_id"]) for d in due] == [("deliver", deal_id)]
    assert client.get("/pledges", params={"due": "true"}).status_code == 401
    mine = client.get(f"/agents/{m}/quests", headers=as_(m)).json()
    assert [d["do"] for d in mine["due"]] == ["deliver"], "my_quests carries the due list for the agent itself"
    set_session(deal_id, "active", {"buyer": c, "seller": m, "status": "delivered",
                                    "delivered_at": datetime.now(UTC).isoformat()})
    sheet = client.get(f"/agents/{m}/sheet").json()
    assert sheet["pledges"]["kept"] == 1
    assert c not in json.dumps(sheet["pledges"]) and deal_id not in json.dumps(sheet["pledges"])


def test_a_refund_pledge_is_attested_once_by_the_buyer_and_contested_by_the_seller(client):
    m, c = agents(client, "m", "c")
    deal_id = session("emporia:service:v1", [c, m], {"buyer": c, "seller": m, "status": "accepted"})
    p = make(client, m, c, "refund", deal_id).json()
    assert attest(client, c, p["pledge_id"], "attest", "broken").status_code == 409, "not before the deal ends"
    set_session(deal_id, "completed", {"buyer": c, "seller": m, "status": "confirmed"})
    assert attest(client, m, p["pledge_id"], "attest", "kept").status_code == 403, "not the maker's to attest"
    assert attest(client, c, p["pledge_id"], "attest", "maybe").status_code == 422
    assert attest(client, m, p["pledge_id"], "contest").status_code == 409, "nothing to contest yet"
    r = attest(client, c, p["pledge_id"], "attest", "broken")
    assert r.status_code == 200 and r.json()["status"] == "broken" and r.json()["contest_by"]
    assert attest(client, c, p["pledge_id"], "attest", "kept").status_code == 409, "final"
    r = attest(client, m, p["pledge_id"], "contest")
    assert r.status_code == 200 and r.json()["status"] == "disputed"
    assert client.get(f"/agents/{m}/sheet").json()["pledges"]["disputed"] == 1


def test_own_operator_pledges_are_made_but_count_nothing(client):
    m, m2 = agents(client, "m", "m2")
    same_operator(m, m2)
    sid = session("emporia:blotto:v1", [m2, m], {"phase": "commit"})
    p = make(client, m, m2, "tell", sid, {"tell": "even_split"}).json()
    record_action(sid, m, "commit")
    record_action(sid, m, "reveal", [4, 3, 3])
    set_session(sid, "completed", {"phase": "commit"})
    assert client.get(f"/pledges/{p['pledge_id']}").json()["status"] == "kept"
    word = client.get(f"/agents/{m}/sheet").json()["pledges"]
    assert word["kept"] == 0 and word["not_counted"] == 1 and any("own operator" in w for w in word["why"])
    quests = client.get(f"/agents/{m}/quests").json()
    assert not next(q for q in quests["quests"] if q["id"] == "keep-word")["done"]


def test_with_the_knob_on_kept_word_moves_trust(client, monkeypatch):
    m, c = agents(client, "m", "c")
    sid = session("emporia:blotto:v1", [c, m], {"phase": "commit"})
    make(client, m, c, "tell", sid, {"tell": "even_split"})
    record_action(sid, m, "commit")
    record_action(sid, m, "reveal", [4, 3, 3])
    set_session(sid, "completed", {"phase": "commit"})
    off = client.get(f"/agents/{m}/sheet").json()
    monkeypatch.setenv("EMPORIA_PLEDGES_TRUST", "1")
    on = client.get(f"/agents/{m}/sheet").json()
    assert on["pledges"]["in_trust"] and on["pledges"]["trust_points"] > 0
    assert on["trust"]["score"] > off["trust"]["score"] and on["formula"].endswith(PL.TRUST_FORMULA)


def test_the_log_takes_both_the_pledge_and_its_acts(client):
    from emporia import translog
    assert {"pledge", "pledge_act"} <= set(translog.SOURCES)
    assert translog.SOURCES["pledge"][0] == "pledges" and translog.SOURCES["pledge_act"][0] == "pledge_acts"


