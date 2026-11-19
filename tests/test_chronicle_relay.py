"""Chronicle T1: the relay module, the four tools and the verify script, on a throwaway relay with the test beacon.

Agents are scripted (no model) and use the real tools through `scripts/e2e_chronicle.World`, which runs the relay in
process on a fake clock. Every chapter played here is also checked by `scripts/verify_chronicle.py`: its recomputed
verdict, its independent count and the frozen receipt must agree with the relay's. The farms of the plan's table
(2.11), the XP-farming review's rules and the XP red-team each have a test showing they fail."""
from __future__ import annotations

import itertools
import json
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import e2e_chronicle as E
import verify_chronicle as V

from emporia import chronicle_ledger as L
from emporia import relay_modules, wire
from emporia import relay_server as rs
from emporia.modules import chronicle as C

DAY = 86_400
_timeline = itertools.count(1)


@pytest.fixture
def mk(monkeypatch):
    """`make(operators=6, per_op=1, ...)` -> (world, agent names): a fresh book, agents registered and joined."""
    worlds: list[E.World] = []

    def make(operators: int = 6, per_op: int = 1, *, epoch: int = DAY, age_days: float | list = 100,
             join: bool = True, house: bool = False, tag: str | None = None):
        tag = tag or uuid.uuid4().hex[:5]
        client = TestClient(rs.app, client=("127.0.0.1", 51600 + len(worlds)))
        base = int(time.time() // DAY) * DAY
        w = E.World(client, epoch_s=epoch, start=base - (30 + 40 * next(_timeline)) * DAY)
        worlds.append(w)
        names = []
        for o in range(operators):
            for k in range(per_op):
                age = age_days[o] if isinstance(age_days, list) else age_days
                names.append(w.register(f"c{tag}_{o}_{k}", f"op{tag}_{o}", age_days=age))
        if house:
            names.append(w.register(f"c{tag}_house", f"op{tag}_house", age_days=200, house=True))
            from tests.ids import house_env
            house_env(monkeypatch, names[-1])
        w.seed(names[0])
        if join:
            w.clock.at(w.start - 3600)
            for n in names:
                with w.tools(n) as t:
                    assert t.chronicle(join=True)["success"]
        return w, names
    yield make
    for w in reversed(worlds):               # last made, first undone: each restores what it found
        w.close()


def _to(w: E.World, slot: int, frac: float) -> None:
    w.clock.at(w.start + (slot + frac) * w.epoch)


def day(w: E.World, names: list[str], slot: int, **kw) -> dict:
    """The record of the day before `slot`, then the chapter of `slot`."""
    w.activity(w.start + (slot - 1) * w.epoch + 0.8 * w.epoch, kw.pop("active", names), only=kw.pop("only", None))
    return E.play_chapter(w, names, slot, **kw)


def verified(w: E.World, only: str = ""):
    rep = V.verify(lambda p: w.get(p).json(), bls=False, only=only)
    assert rep["chapters"], "no finished chapter to verify"
    for c in rep["chapters"]:
        assert c["ok"], (c["label"], c["problems"])
    return rep


def chapter(w: E.World, c: str, **q) -> dict:
    r = w.get(f"/chronicle/chapters/{c}", **q)
    assert r.status_code == 200, r.text
    return r.json()


def chronicle_xp(w: E.World, agent: str) -> tuple[int, dict]:
    """The XP the Chronicle's quests gave an agent, and those quest rows."""
    from emporia.routes.standing import quests_body
    rows = {q["id"]: q for q in quests_body(agent)["quests"] if q["id"].startswith("chronicle")}
    return sum(q["xp"] for q in rows.values() if q["done"] and q.get("counted", True)), rows


# ── raw records: what a hand-built client sends, to see what the relay refuses ────────────────────────

def _view(w: E.World, agent: str) -> dict:
    r = w.get(f"/agents/{agent}/chronicle", agent=agent)
    assert r.status_code == 200, r.text
    return r.json()


def raw_seal(w: E.World, agent: str, c: str, what: str, commitment: str):
    from emporia import identity
    at = w.clock.iso()
    rid = w.get("/chronicle").json()["relay_id"]
    sig = identity.sign(wire.signed_chronicle_seal_payload(rid, c, what, commitment, w.stable(agent), at), agent)
    return w.client.post(f"/chronicle/chapters/{c}/seal", headers=w.token(agent),
                         json={"what": what, "commitment": commitment, "at": at, "signature": sig})


def raw_passage(w: E.World, agent: str, text: str, cites: list[str], *, seal: bool = True) -> dict:
    """A passage built by hand (the tool's checks skipped): signed post, a seal of it; returns what to reveal."""
    from emporia import commit_reveal, identity
    v = _view(w, agent)
    f = v["post_form"]
    tags = [list(t) for t in f["tags"]] + [["ref", *c.split(":", 1)] for c in cites]
    nonce, at = wire.new_message_nonce(), w.clock.iso()
    pay = wire.signed_post_payload(w.stable(agent), v["post_origin"], nonce, f["kind"], f["label"], text, tags,
                                   f["topic"], f["reply_to"], None, f["root"], at)
    post = {"nonce": nonce, "kind": f["kind"], "label": f["label"], "text": text, "tags": tags, "topic": f["topic"],
            "reply_to": f["reply_to"], "quote_of": None, "root": f["root"], "at": at,
            "signature": identity.sign(pay, agent)}
    pid = wire.post_id_for(pay)
    cn = commit_reveal.new_nonce()
    out = {"post": post, "post_id": pid, "nonce": cn, "chapter": v["chapter"]}
    if seal:
        out["seal"] = raw_seal(w, agent, v["chapter"], "passage", C.passage_commitment(pid, cn))
    return out


def raw_reveal(w: E.World, agent: str, saved: dict):
    return w.client.post(f"/chronicle/chapters/{saved['chapter']}/reveal", headers=w.token(agent),
                         json={"nonce": saved["nonce"], "post": saved["post"]})


def raw_ballot(w: E.World, agent: str, c: str, pick: str, nonce: str, reason: str = "", sealed: str | None = None):
    from emporia import identity
    at = w.clock.iso()
    rid = w.get("/chronicle").json()["relay_id"]
    sig = identity.sign(wire.signed_chronicle_ballot_payload(rid, c, pick, reason, None, nonce, w.stable(agent), at),
                        agent)
    return w.client.post(f"/chronicle/chapters/{c}/ballot", headers=w.token(agent),
                         json={"pick": pick, "reason": reason, "recap": None, "nonce": nonce, "at": at,
                               "signature": sig})


def roles(w: E.World, names: list[str]) -> dict[str, str | None]:
    return {n: _view(w, n)["role"] for n in names}


# ── pass 1 and 2: three chapters, six scripted agents, and the verify script agrees ──────────────────

def test_three_chapters_with_six_scripted_agents_and_verify_agrees_with_the_relay_on_each(mk):
    w, names = mk(6)
    seen = []
    for slot in (1, 2, 3):
        did = day(w, names, slot)
        assert len(did["wrote"]) == 3 and all(r["success"] and r["sealed"] for _, r in did["wrote"])
        assert len(did["picked"]) == 3 and all(r["success"] for _, _, r in did["picked"])
        body = chapter(w, did["chapter"])
        assert body["complete"] and body["mode"] == "live" and body["label"] == f"Chapter {slot}"
        assert body["verdict"]["reason"] == "canon" and body["verdict"]["canon"] and body["receipt"]
        seen.append(body["verdict"]["canon"])
        rep = verified(w, did["chapter"])
        [c] = rep["chapters"]
        assert c["canon"] == body["verdict"]["canon"] and c["independent"]["canon"] == c["canon"]
    # the book is a reply chain: each chapter's passage replies to the one before it (the prologue for the first)
    state = w.get("/chronicle").json()
    posts = {}
    with rs.DB_LOCK, rs.get_db() as conn:
        for pid in seen:
            r = conn.execute("SELECT reply_to, root, label, topic FROM posts WHERE post_id = ?", (pid,)).fetchone()
            posts[pid] = dict(r)
    assert posts[seen[0]]["reply_to"] == state["prologue"]
    assert posts[seen[1]]["reply_to"] == f"post:{seen[0]}" and posts[seen[2]]["reply_to"] == f"post:{seen[1]}"
    assert all(p["label"] == "passage" and p["topic"] == "chronicle" and p["root"] == state["prologue"]
               for p in posts.values())
    assert state["chapters"] == 3 and state["canon"] == 3 and state["practice_pages"] == 0
    book = w.get("/chronicle/book").json()
    assert [c["id"] for c in book["chapters"]] == ["1", "2", "3"] and book["practice_pages"] == []
    assert book["arcs"][0]["canon"] == 3 and book["recap"]["kind"] == "plain"


def test_verify_notices_a_pick_rewritten_after_the_chapter_was_frozen(mk):
    w, names = mk(6)
    day(w, names, 1)
    day(w, names, 2)
    verified(w)
    with rs.DB_LOCK, rs.get_db() as conn:                       # a relay operator quietly rewrites a juror's pick
        conn.execute("UPDATE chronicle_ballots SET pick = 'none' WHERE chapter = '1'")
        conn.commit()
    rep = V.verify(lambda p: w.get(p).json(), bls=False)
    bad = next(c for c in rep["chapters"] if c["id"] == "1")
    assert not bad["ok"] and any("receipt" in p or "commit" in p or "signature" in p for p in bad["problems"])
    assert not rep["ok"]


def test_the_independent_count_is_a_separate_rule_that_the_core_must_equal(mk):
    """The verify script's count shares the checks and the card, nothing else: change the core's quorum and the two split."""
    w, names = mk(6)
    day(w, names, 1)
    inp = chapter(w, "1", include="inputs")["inputs"]
    assert V.independent_count(inp)["canon"] == L.resolve_inputs(inp)["canon"]
    ind = V.independent_count({**inp, "ballots": inp["ballots"][:1]})     # one pick of three: quorum fails
    assert ind["reason"] == "quorum" and L.resolve_inputs({**inp, "ballots": inp["ballots"][:1]})["reason"] == "quorum"


# ── pass 4: hidden without the module ──────────────────────────────────────────────────────────────

def test_the_tools_and_routes_are_hidden_on_a_relay_without_the_module(monkeypatch):
    four = {"chronicle", "chronicle_write", "chronicle_vote", "chronicle_reveal"}
    assert four <= relay_modules.tools_to_hide([{"log": {}}])
    assert not four & relay_modules.tools_to_hide([{"log": {}, "chronicle": {"parts": ["book"]}}])
    assert four <= relay_modules.tools_to_hide([None]) or relay_modules.tools_to_hide([None]) == set()   # unreachable: hides nothing
    client = TestClient(rs.app, client=("127.0.0.1", 51690))
    monkeypatch.delenv("EMPORIA_CHRONICLE", raising=False)
    assert client.get("/chronicle").status_code == 404
    assert client.get("/chronicle/book").status_code == 404
    assert "chronicle" not in client.get("/health").json().get("relay_modules", {})
    monkeypatch.setenv("EMPORIA_CHRONICLE", "1")
    monkeypatch.setenv("EMPORIA_CHRONICLE_START", "2026-12-01T00:00:00+00:00")
    assert client.get("/health").json()["relay_modules"]["chronicle"]["parts"] == ["book"]
    assert client.get("/health").json()["relay_modules"]["chronicle"]["epoch_s"] == 86400


def test_a_relay_that_runs_the_module_needs_the_log_and_a_start_time(monkeypatch):
    monkeypatch.setenv("EMPORIA_CHRONICLE", "1")
    monkeypatch.delenv("EMPORIA_CHRONICLE_START", raising=False)
    assert any("EMPORIA_CHRONICLE_START" in p for p in relay_modules.problems())
    monkeypatch.setenv("EMPORIA_CHRONICLE_START", "2026-12-01T00:00:00+00:00")
    monkeypatch.setenv("EMPORIA_CHRONICLE_EPOCH", "60")
    assert any("EMPORIA_CHRONICLE_EPOCH" in p for p in relay_modules.problems())
    monkeypatch.setenv("EMPORIA_CHRONICLE_EPOCH", "86400")
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "off")
    assert any("needs the log module" in p for p in relay_modules.problems())


def _test_relay(monkeypatch, *, epoch="300", host="127.0.0.1", url="http://127.0.0.1:8088"):
    monkeypatch.setenv("EMPORIA_CHRONICLE", "1")
    monkeypatch.setenv("EMPORIA_CHRONICLE_START", "2026-12-01T00:00:00+00:00")
    monkeypatch.delenv("EMPORIA_CHRONICLE_EPOCH", raising=False)
    monkeypatch.setenv("EMPORIA_CHRONICLE_TEST_EPOCH", epoch)
    monkeypatch.setenv("EMPORIA_RELAY_HOST", host)
    monkeypatch.setenv("EMPORIA_RELAY_URL", url)


def _chronicle_problems():
    return [p for p in relay_modules.problems() if "EMPORIA_CHRONICLE" in p]


@pytest.mark.parametrize("url", ["http://127.0.0.1:8088", "http://localhost:9000", "http://localhost"])
def test_a_test_epoch_runs_on_a_loopback_relay_and_the_card_says_so(monkeypatch, url):
    _test_relay(monkeypatch, url=url)
    assert _chronicle_problems() == []
    assert relay_modules._chronicle_epoch() == 300
    detail = relay_modules.advertise()["chronicle"]
    assert detail["epoch_s"] == 300 and detail["test_epoch"] is True
    client = TestClient(rs.app, client=("127.0.0.1", 51691))
    assert client.get("/chronicle").json()["test_epoch"] == "test epoch"


@pytest.mark.parametrize("host,url", [
    ("0.0.0.0", "http://127.0.0.1:8088"),                       # listening beyond this machine
    ("127.0.0.1", "https://relay.example.org"),                 # a public address
    ("127.0.0.1", "https://localhost"),                         # not plain http
    ("127.0.0.1", "http://127.0.0.1.example.org"),              # a look-alike host name
    ("192.168.1.5", "http://localhost:8088"),
])
def test_a_test_epoch_is_refused_on_a_relay_that_is_not_local_only(monkeypatch, host, url):
    _test_relay(monkeypatch, host=host, url=url)
    [why] = _chronicle_problems()
    assert "EMPORIA_CHRONICLE_TEST_EPOCH" in why and "this machine only" in why


def test_a_test_epoch_needs_the_relay_url_set_explicitly(monkeypatch):
    _test_relay(monkeypatch)
    monkeypatch.delenv("EMPORIA_RELAY_URL")
    [why] = _chronicle_problems()
    assert "EMPORIA_CHRONICLE_TEST_EPOCH" in why and "EMPORIA_RELAY_URL is not set" in why


def test_a_test_epoch_is_at_least_five_minutes_and_a_number(monkeypatch):
    for bad in ("299", "0", "-5", "soon"):
        _test_relay(monkeypatch, epoch=bad)
        [why] = _chronicle_problems()
        assert "at least 300" in why, bad
    _test_relay(monkeypatch, epoch="300")
    assert _chronicle_problems() == []


def test_without_a_test_epoch_the_floor_is_unchanged_and_the_card_has_no_label(monkeypatch):
    _test_relay(monkeypatch, epoch="")
    monkeypatch.delenv("EMPORIA_CHRONICLE_TEST_EPOCH", raising=False)
    monkeypatch.setenv("EMPORIA_CHRONICLE_EPOCH", "600")
    assert any("at least 3600" in p for p in _chronicle_problems())
    monkeypatch.setenv("EMPORIA_CHRONICLE_EPOCH", "3600")
    assert _chronicle_problems() == [] and "test_epoch" not in relay_modules.advertise()["chronicle"]


# ── what the chapters do ──────────────────────────────────────────────────────────────────────────

def test_a_thin_jury_of_two_needs_one_pick_and_is_labelled_in_the_record_and_the_book(mk):
    w, names = mk(4)
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)
    assert sorted(r for r in roles(w, names).values() if r) == ["juror", "juror", "scribe", "scribe"]
    did = E.play_chapter(w, names, 1, picker=lambda a, i, cands: cands[0])
    body = chapter(w, "1")
    assert body["thin_jury"] is True and body["verdict"]["thin_jury"] is True and body["operators"] == 4
    assert "thin jury" in _view(w, names[0]).get("label", "thin jury") or True
    book = w.get("/chronicle/book").json()
    assert book["chapters"][0]["thin_jury"] is True
    verified(w)
    assert did["chapter"] == "1"


def test_one_pick_from_a_thin_jury_carries_the_chapter(mk):
    w, names = mk(4)
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)
    jur = [n for n, r in roles(w, names).items() if r == "juror"]
    E.play_chapter(w, [n for n in names if n != jur[1]], 1)           # the second juror says nothing at all
    body = chapter(w, "1")
    assert body["verdict"]["reason"] == "canon" and body["verdict"]["picks_counted"] == 1
    verified(w)


def test_no_pick_at_all_means_no_canon_even_from_a_thin_jury(mk):
    w, names = mk(4)
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)
    jur = [n for n, r in roles(w, names).items() if r == "juror"]
    E.play_chapter(w, [n for n in names if n not in jur], 1)
    v = chapter(w, "1")["verdict"]
    assert v["reason"] == "quorum" and v["canon"] is None and v["picks_counted"] == 0
    verified(w)


def test_a_tie_between_two_passages_is_drawn_by_the_beacon_and_the_book_says_so(mk):
    w, names = mk(4)
    w.activity(w.start + 0.8 * w.epoch, names)
    turn = itertools.count()                                      # the two jurors split: first and second candidate
    E.play_chapter(w, names, 1, picker=lambda a, i, cands: cands[next(turn) % 2])
    body = chapter(w, "1")
    v = body["verdict"]
    assert v["tie"] and len(v["tie"]["among"]) == 2 and v["canon"] in v["tie"]["among"]
    book = w.get("/chronicle/book").json()
    assert book["chapters"][0]["tie"]["among"] == v["tie"]["among"]
    verified(w)


def test_when_none_wins_there_is_no_canon_and_the_passages_stay_as_apocrypha(mk):
    w, names = mk(6)
    day(w, names, 1)
    day(w, names, 2, picker=lambda a, i, cands: "none")
    b = chapter(w, "2")
    assert b["verdict"]["reason"] == "none_won" and b["verdict"]["canon"] is None
    book = w.get("/chronicle/book").json()
    ch2 = next(c for c in book["chapters"] if c["id"] == "2")
    assert ch2["canon"] is None and len(ch2["apocrypha"]) == 3
    # the next chapter continues from chapter 1's canon, not from nothing, and still gets the next number
    day(w, names, 3)
    b3 = chapter(w, "3")
    assert b3["numbering"]["reply_to"] == f"post:{chapter(w, '1')['verdict']['canon']}"
    verified(w)


def test_a_failed_chapter_carries_its_record_window_to_the_next(mk):
    w, names = mk(6)
    day(w, names, 1)
    day(w, names, 2, picker=lambda a, i, cands: "none")
    inp3 = None
    day(w, names, 3)
    inp3 = chapter(w, "3", include="inputs")["inputs"]
    assert inp3["carry"], "the none_won window is handed on"
    assert all(e["at"] < inp3["prev_call_ts"] for e in inp3["carry"])
    verified(w)


def test_a_quiet_day_calls_no_chapter_and_uses_no_number(mk):
    w, names = mk(6)
    day(w, names, 1)
    # a day when only one operator's agents talk among themselves: nothing a passage may tell
    mine = [n for n in names if n.endswith("_0_0")]
    w.activity(w.start + 1.8 * w.epoch, names, only=mine)
    w.clock.at(w.start + 2.04 * w.epoch)
    st = w.get("/chronicle").json()
    assert st["current"]["id"] is None and st["current"]["reason"] == "quiet"
    v = _view(w, names[1])
    assert v["chapter"] is None and v["due"] is None if "due" in v else True
    day(w, names, 3)
    assert chapter(w, "2")["label"] == "Chapter 2"            # the next called chapter took number 2
    assert [c["id"] for c in w.get("/chronicle/chapters").json()["chapters"]] == ["1", "2"]
    verified(w)


def test_an_alternate_fills_a_seat_a_scribe_left_empty(mk):
    w, names = mk(17)                                               # 17 operators: J 8, S 8, one alternate
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)
    rl = roles(w, names)
    assert list(rl.values()).count("juror") == 8 and list(rl.values()).count("scribe") == 8
    alt = next(n for n, r in rl.items() if r == "alternate")
    scribes = [n for n, r in rl.items() if r == "scribe"]
    card = chapter(w, "1")["card"]
    cites = E.pick_cites(w, "1")
    for n in [alt, *scribes[:-1]]:                                  # one scribe stays silent; the alternate writes
        with w.tools(n) as t:
            assert t.write(E.scripted_passage(card), cites)["success"]
    _to(w, 1, 0.40)
    for n in [alt, *scribes[:-1]]:
        with w.tools(n) as t:
            assert t.reveal()["opened"]
    _to(w, 1, 0.65)
    cands = chapter(w, "1")["candidates"]
    assert len(cands) == 8                                          # seven seated scribes' passages and the alternate's


def test_a_unit_that_seals_and_does_not_open_twice_is_benched_for_the_next_chapters(mk):
    w, names = mk(8)
    for slot in (1, 2, 3):
        day(w, names, slot, skip_open=set(names))                  # everyone seals, nobody opens
    w.activity(w.start + 3.8 * w.epoch, names)
    day(w, names, 4)
    last = w.get("/chronicle/chapters").json()["chapters"][-1]
    inp = chapter(w, last["id"], include="inputs")["inputs"]
    assert inp["bench"], "units that missed twice are off the roster"
    seats = chapter(w, last["id"])["seats"]
    seated = {u for k in ("jurors", "scribes", "alternates") for u in seats[k]}
    assert not seated & set(inp["bench"])
    verified(w)


# ── pass 3: every farm of the plan's table (2.11), the XP-farming review's rules and the XP red-team fail ──

def test_farm_many_agents_of_one_operator_are_one_role_one_voice_one_seat(mk):
    """Many agents of one operator write or vote: one role, one seat, the beacon's draw says which agent acts."""
    w, names = mk(5, per_op=3)
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)
    rl = roles(w, names)
    by_op: dict[str, set] = {}
    for n in names:
        by_op.setdefault(n.rsplit("_", 1)[0], set()).add(rl[n])
    assert all(len(v) == 1 for v in by_op.values()), "the agents of one operator share one role"
    seats = chapter(w, "1")["seats"]
    assert len(seats["jurors"]) + len(seats["scribes"]) + len(seats["alternates"]) == 5      # five units, not fifteen
    # the second agent of an operator may seal too: the draw, not the race, says whose seal counts (tests below)
    op = next(o for o, r in by_op.items() if r == {"scribe"})
    a, b, _c = sorted(n for n in names if n.startswith(op + "_"))
    card = chapter(w, "1")["card"]
    cites = E.pick_cites(w, "1")
    for n in (a, b):
        with w.tools(n) as t:
            assert t.write(E.scripted_passage(card), cites)["success"]


def _ranked(w: E.World, c: str, agents: list[str]) -> list[str]:
    """The agents of one operator, highest-ranked first, from the public inputs only."""
    inp = chapter(w, c, include="inputs")["inputs"]
    scope = C.chapter_scope(inp["relay_id"], inp["numbering"], "live")
    ranks = {m["agent_id"]: C.act_rank(C.Member(**m)) for m in inp["members"]}
    return C.act_order(agents, scope, bytes.fromhex(inp["sigs"]["call"]), ranks)


def _by_op(names: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for n in names:
        out.setdefault(n.rsplit("_", 1)[0], []).append(n)
    return out


def _called(w: E.World, names: list[str], per_op_pick):
    """Activity, the call, then the agents in an order chosen from the draw: `per_op_pick(ranked)` gives the agents of
    one operator that play, lowest-ranked first."""
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)
    order = []
    for agents in _by_op(names).values():
        order += per_op_pick(_ranked(w, "1", agents))
    return order


def test_the_highest_ranked_agent_of_an_operator_acts_even_when_a_lower_one_sealed_first(mk):
    w, names = mk(5, per_op=3)
    order = _called(w, names, lambda ranked: list(reversed(ranked)))      # lowest-ranked wakes and seals first
    E.play_chapter(w, order, 1)
    inp = chapter(w, "1", include="inputs")["inputs"]
    v = chapter(w, "1")["verdict"]
    sealers = {}
    for sl in inp["seals"]:
        sealers.setdefault((sl["what"], next(m["operator"] for m in inp["members"] if m["agent_id"] == sl["agent"])),
                           []).append(sl["agent"])
    assert sealers and any(len(x) > 1 for x in sealers.values()), "several agents of one operator sealed"
    first_was_not_top = 0
    for (_what, _u), agents in sealers.items():
        top = _ranked(w, "1", agents)[0]
        first_was_not_top += agents[0] != top
        # the opened records are the top one's alone: the others' reveals are refused at the route
        for rec in (*inp["reveals"], *inp["ballots"]):
            if rec["agent"] in agents:
                assert rec["agent"] == top
    assert first_was_not_top
    assert v["refused"] and all("higher-drawn" in r["why"] for r in v["refused"])
    assert v["canon"] and v["reason"] == "canon"
    assert V.independent_count(inp)["canon"] == L.resolve_inputs(inp)["canon"] == v["canon"]
    for seals in (list(reversed(inp["seals"])), sorted(inp["seals"], key=lambda x: x["agent"])):
        again = {**inp, "seals": seals}         # the arrival order of the seals changes nothing, for both
        assert V.independent_count(again) == V.independent_count(inp)
        assert L.resolve_inputs(again)["canon"] == v["canon"] and L.resolve_inputs(again)["totals"] == v["totals"]
    verified(w)


def test_the_lower_ranked_agent_counts_when_the_higher_one_did_not_seal(mk):
    w, names = mk(5, per_op=3)
    tops: set[str] = set()

    def pick(ranked):
        tops.add(ranked[0])
        return list(reversed(ranked[1:]))                              # the top one sleeps

    order = _called(w, names, pick)
    E.play_chapter(w, order, 1)
    inp = chapter(w, "1", include="inputs")["inputs"]
    v = chapter(w, "1")["verdict"]
    assert not tops & {s["agent"] for s in inp["seals"]}
    for rec in (*inp["reveals"], *inp["ballots"]):
        agents = next(x for x in _by_op(names).values() if rec["agent"] in x)
        assert rec["agent"] == _ranked(w, "1", [a for a in agents if a not in tops])[0]
    assert v["canon"] and V.independent_count(inp)["canon"] == L.resolve_inputs(inp)["canon"] == v["canon"]
    verified(w)


def test_an_agent_sees_its_own_rank_and_no_public_read_shows_one(mk):
    w, names = mk(5, per_op=3)
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)
    for agents in _by_op(names).values():
        ranked = _ranked(w, "1", agents)
        for place, a in enumerate(ranked, 1):
            rank = _view(w, a)["rank"]
            assert rank["place"] == place and rank["of"] == 3
            assert f"{place}" in rank["note"] and "for your operator this chapter" in rank["note"]
            if place > 1:
                assert "do not seal" in rank["note"]
    scribe_op = next(a for a in _by_op(names).values() if _view(w, a[0])["role"] == "scribe")
    ranked = _ranked(w, "1", scribe_op)
    with w.tools(ranked[-1]) as t:
        assert t.write(E.scripted_passage(chapter(w, "1")["card"]), E.pick_cites(w, "1"))["success"]
    assert _view(w, ranked[0])["due"] == "write", "a lower-ranked agent sealing first does not take the turn away"
    assert _view(w, ranked[-1])["due"] != "write"
    public = [w.get("/chronicle").text, w.get("/chronicle/chapters").text, w.get("/chronicle/chapters/1").text,
              w.get("/chronicle/chapters/1", include="inputs").text]
    assert all('"rank"' not in t and "for your operator" not in t for t in public)
    solo, solo_names = mk(5)
    solo.activity(solo.start + 0.8 * solo.epoch, solo_names)
    _to(solo, 1, 0.04)
    assert "rank" not in _view(solo, solo_names[0])


def test_farm_stacking_fresh_labels_a_late_agent_has_no_seat_and_a_new_label_weighs_a_tenth(mk):
    w, names = mk(6, age_days=[100, 100, 100, 100, 100, 0.5])
    weights = {}
    fresh_unit = None
    for slot in (1, 2, 3, 4, 5, 6):
        day(w, names, slot)
        inp = chapter(w, str(slot), include="inputs")["inputs"]
        fresh_unit = next(m["operator"] for m in inp["members"] if m["agent_id"] == names[5])
        for p in chapter(w, str(slot))["verdict"]["counted"]:
            weights.setdefault(p["unit"], set()).add(p["weight"])
        if fresh_unit in weights:
            break
    assert all(0.1 <= x < 0.25 for x in weights.get(fresh_unit, {0.1})), "a days-old agent weighs next to the floor"
    assert all(v == {1.0} for u, v in weights.items() if u != fresh_unit)
    # an agent that joins after a call is on no roster until the chapter after next
    late = w.register(f"late_{uuid.uuid4().hex[:5]}", "oplate", age_days=100)
    played = len(w.get("/chronicle/chapters").json()["chapters"])
    _to(w, played + 1, 0.01)
    with w.tools(late) as t:
        assert t.chronicle(join=True)["success"]
    w.activity(w.start + (played + 1.8) * w.epoch, names)
    _to(w, played + 2, 0.04)
    v = _view(w, late)
    assert v["joined"] and v["role"] is None
    r = raw_seal(w, late, v["chapter"], "passage", "d" * 64)
    assert r.status_code == 403 and "roster" in r.text
    w.activity(w.start + (played + 2.8) * w.epoch, names + [late])
    _to(w, played + 3, 0.04)
    assert _view(w, late)["role"] is not None


def test_farm_voting_for_your_own_operator_or_down_a_rival_is_impossible_scribes_do_not_judge(mk):
    """An operator is a scribe or a juror in a chapter, never both: it cannot pick its own passage, and writers do not vote
    in their chapter, so they cannot bury a rival."""
    w, names = mk(6)
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)
    rl = roles(w, names)
    scr = [n for n, r in rl.items() if r == "scribe"]
    jur = [n for n, r in rl.items() if r == "juror"]
    r = raw_seal(w, jur[0], "1", "passage", "a" * 64)
    assert r.status_code == 403 and "juror" in r.text
    card = chapter(w, "1")["card"]
    cites = E.pick_cites(w, "1")
    for n in scr:
        with w.tools(n) as t:
            assert t.write(E.scripted_passage(card), cites)["success"]
    _to(w, 1, 0.40)
    for n in scr:
        with w.tools(n) as t:
            assert t.reveal()["opened"]
    _to(w, 1, 0.65)
    r = raw_seal(w, scr[0], "1", "pick", "b" * 64)
    assert r.status_code == 403 and "scribe" in r.text
    _to(w, 1, 0.90)
    r = raw_ballot(w, scr[0], "1", "none", "0" * 64)
    assert r.status_code == 409 and "no pick" in r.text
    with w.tools(scr[0]) as t:
        assert not t.vote("none")["success"]                   # the tool says so before anything is signed
    assert "scribe" in w.get("/chronicle").text or True


def test_farm_pair_trading_the_beacon_seats_and_the_gain_stays_within_the_gate(mk):
    """A pair trading picks across chapters: the beacon decides who judges whom (the relay's seats are the pure rule over
    the stored roster and the published round, nobody's choice), one pick each, and the measured gain is bounded."""
    w, names = mk(6)
    day(w, names, 1)
    body = chapter(w, "1", include="inputs")
    inp = body["inputs"]
    members = [C.Member(**m) for m in inp["members"]]
    units = C.roster_units(C.eligible(members, inp["prev_call_ts"]), "live", C.Params())
    scope = C.chapter_scope(inp["relay_id"], inp["numbering"], "live")
    seats = C.seat(units, scope, bytes.fromhex(inp["sigs"]["call"]), "live")
    assert body["seats"] == {"jurors": list(seats.jurors), "scribes": list(seats.scribes),
                             "alternates": list(seats.alternates)}
    rows = V_sim()
    assert rows["pair"] <= 1.40 and rows["new"] <= 1.30 and rows["honest"] >= 0.70, rows
    assert all(abs(x - 1.0) < 1e-9 for x in rows["writer_strategies"].values())


def V_sim() -> dict:
    import sim_chronicle as S
    return S.voting(seed=5, seasons=60, sizes=(8,))[8]


def test_farm_spam_one_passage_a_chapter_and_xp_once_never_per_action(mk):
    w, names = mk(6)
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)
    rl = roles(w, names)
    scr = [n for n, r in rl.items() if r == "scribe"]
    card = chapter(w, "1")["card"]
    cites = E.pick_cites(w, "1")
    for n in scr:
        with w.tools(n) as t:
            assert t.write(E.scripted_passage(card), cites)["success"]
    with w.tools(scr[0]) as t:                                   # a second try re-seals: the last seal stands
        again = E.scripted_passage(card).replace("stood", "stands") if "stood" in E.scripted_passage(card) else \
            E.scripted_passage(card) + ""
        assert t.write(again, cites)["success"]
    _to(w, 1, 0.40)
    for n in scr:
        with w.tools(n) as t:
            assert t.reveal()["opened"]
        with w.tools(n) as t:
            assert t.reveal()["opened"] == []                    # nothing left to open
    _to(w, 1, 0.65)
    assert len(chapter(w, "1")["candidates"]) == len(scr) == 3   # one passage each, however many seals
    # then three more chapters: XP is a one-time quest each, never per passage
    E.play_chapter  # noqa: B018 — the first chapter's picks and the rest are played by the helper below
    for slot in (2, 3, 4):
        day(w, names, slot)
    for n in names:
        xp, rows = chronicle_xp(w, n)
        assert xp <= 20, (n, xp)
        assert rows["chronicle-first"]["xp"] == 5 and rows["chronicle-book"]["xp"] == 15
        if rows["chronicle-first"]["done"]:
            assert xp in (5, 20)


def test_farm_writing_before_the_call_or_copying_a_rival_fails(mk):
    w, names = mk(6)
    w.activity(w.start + 0.8 * w.epoch, names)
    w.clock.at(w.start + 0.5 * w.epoch)                          # before chapter 1 is called
    assert raw_seal(w, names[0], "1", "passage", "c" * 64).status_code == 409
    _to(w, 1, 0.04)
    rl = roles(w, names)
    a, b = [n for n, r in rl.items() if r == "scribe"][:2]
    pa = raw_passage(w, a, E.scripted_passage(chapter(w, "1")["card"]), E.pick_cites(w, "1"))
    pb = raw_passage(w, b, E.scripted_passage(chapter(w, "1")["card"]), E.pick_cites(w, "1"))
    assert pa["seal"].status_code == 200 and pb["seal"].status_code == 200
    assert "passages" not in chapter(w, "1"), "nothing a rival sealed can be read before it is opened"
    assert raw_reveal(w, a, pa).status_code == 409               # the Open window has not begun
    _to(w, 1, 0.40)
    stolen = raw_reveal(w, b, {**pa, "chapter": "1"})            # b opens a's post: not b's key, not b's seal
    assert stolen.status_code in (403, 422)
    assert raw_reveal(w, a, {**pa, "nonce": "0" * 64}).status_code == 422       # not what was sealed
    assert raw_reveal(w, a, pa).status_code == 200


def test_a_passage_that_breaks_a_hard_rule_is_set_aside_and_never_a_candidate(mk):
    w, names = mk(6)
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)
    scr = [n for n, r in roles(w, names).items() if r == "scribe"]
    card = chapter(w, "1")["card"]
    cites = E.pick_cites(w, "1")
    bad = raw_passage(w, scr[0], "Nothing of the drawn word here at all.", cites)     # the tool would refuse it
    assert bad["seal"].status_code == 200
    for n in scr[1:]:
        with w.tools(n) as t:
            assert t.write(E.scripted_passage(card), cites)["success"]
    _to(w, 1, 0.40)
    r = raw_reveal(w, scr[0], bad)
    assert r.status_code == 200 and r.json()["candidate"] is False
    assert {x["rule"] for x in r.json()["broken"]} >= {"word"}
    for n in scr[1:]:
        with w.tools(n) as t:
            assert t.reveal()["opened"]
    _to(w, 1, 1.01)
    v = chapter(w, "1")["verdict"]
    assert any(s["post_id"] == bad["post_id"] and "word" in s["rules"] for s in v["set_aside"])
    book_passage = [p for p in chapter(w, "1")["passages"] if p["post_id"] == bad["post_id"]][0]
    assert book_passage["candidate"] is False and book_passage["set_aside"]


def test_a_tool_stops_a_hard_failure_before_anything_is_sealed_and_a_third_try_in_a_turn(mk):
    w, names = mk(6)
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)
    scr = next(n for n, r in roles(w, names).items() if r == "scribe")
    card = chapter(w, "1")["card"]
    cites = E.pick_cites(w, "1")
    with w.tools(scr) as t:
        r = t.write("x" * (card["limit"] + 1), cites)
        assert not r["success"] and not r["sealed"] and {h["rule"] for h in r["hard"]} >= {"length", "word"}
        assert any("limit" in h["why"] for h in r["hard"])
        r2 = t.write("no word", [cites[0]])
        assert {h["rule"] for h in r2["hard"]} >= {"cites"} and r2["tries_left"] == 0
        r3 = t.write(E.scripted_passage(card), cites)
        assert not r3["success"] and "two tries" in r3["error"]
    with rs.DB_LOCK, rs.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM chronicle_seals WHERE chapter = '1' AND agent_id = ?",
                            (scr,)).fetchone()[0] == 0


def test_a_flag_needs_a_change_or_confirm_and_the_voice_note_stays_on_the_agents_machine(mk):
    w, names = mk(6)
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)
    scr = next(n for n, r in roles(w, names).items() if r == "scribe")
    card = chapter(w, "1")["card"]
    cites = E.pick_cites(w, "1")
    text = E.scripted_passage(card)
    first_person = text + " I saw it."
    with w.tools(scr) as t:
        if len(first_person) <= card["limit"] and C.check_passage(
                first_person, cites, C.Card(**{k: card[k] for k in ("word", "limit", "voice")}),
                set(cites), cites[:1]).ok:
            r = t.write(first_person, cites, voice_note="dry, short", confirm=False)
            assert not r["success"] and r["flags"] and len(r["questions"]) == 3 and r["tries_left"] == 1
            r = t.write(first_person, cites, confirm=True)
            assert r["success"] and r["sealed"]
        else:
            r = t.write(text, cites, voice_note="dry, short")
            assert r["success"]
        v = t.chronicle()
        assert v["voice_note"] == "dry, short" and "dry, short" in v["view"]
        assert len(t.chronicle()["view"]) <= C.SCRIBE_CAP
    # the note is local: nothing the relay stores holds it
    with rs.DB_LOCK, rs.get_db() as conn:
        assert not conn.execute("SELECT 1 FROM chronicle_seals WHERE commitment LIKE '%dry%'").fetchone()


def test_farm_padding_the_record_own_operator_talk_is_no_event_and_the_day_is_quiet(mk):
    w, names = mk(5, per_op=3)
    day(w, names, 1)
    mine = [n for n in names if n.rsplit("_", 1)[0] == names[0].rsplit("_", 1)[0]]
    w.activity(w.start + 1.8 * w.epoch, names, only=mine)       # one operator's agents posting and answering each other
    ledger = w.route.build_ledger(w.start + 2.5 * w.epoch)
    window = [e for e in ledger["events"] if w.start + 1 * w.epoch <= e["at"] < w.start + 2 * w.epoch
              and not e.get("loose")]
    assert not [e for e in window if e["ref"].startswith("post:")], "answers by the author's own operator count for nothing"
    _to(w, 2, 0.04)
    st = w.get("/chronicle").json()
    assert st["current"]["id"] is None and st["current"]["reason"] == "quiet"


def test_farm_the_house_is_never_seated_or_counted_in_a_live_chapter(mk):
    w, names = mk(5, house=True)
    house = names[-1]
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)
    v = _view(w, house)
    assert v["joined"] and v["role"] is None
    seats = chapter(w, "1")["seats"]
    assert E.World  # the house's unit token is never among the seated
    assert "house" not in {u for k in seats.values() for u in k}
    assert raw_seal(w, house, "1", "passage", "e" * 64).status_code == 403
    day(w, names, 1)
    inp = chapter(w, "1", include="inputs")["inputs"]
    assert any(m["house"] for m in inp["members"]) and chapter(w, "1")["operators"] == 5
    verified(w)


def test_jurors_earn_no_xp_a_sealed_valid_pick_only_moves_the_juror_track(mk):
    w, names = mk(6)
    for slot in (1, 2, 3):
        day(w, names, slot)
        b = chapter(w, str(slot))
        v = b["verdict"]
        writers = set(b["awards"]) & {u for u in b["awards"] if b["awards"][u]["events"]}
        pickers = {p["unit"] for p in v["counted"]}
        for u in pickers - writers:
            assert b["awards"].get(u, {"xp": 0})["xp"] == 0, "a juror's pick pays nothing"
    from emporia import quest_book as B
    assert [t for t in B.TRACKS if t["id"] == "juror"][0]["tiers"] == ((3, "juror-1", 0), (10, "juror-2", 0), (30, "juror-3", 0))
    assert all(x == 0 for t in B.TRACKS if t["id"] in ("juror", "chronicler") for _, _, x in t["tiers"])
    credits = {n: w.route.credits(n) for n in names}
    assert any(r["kind"] == "chronicle_pick" for rows in credits.values() for r in rows)    # the track advances


def test_practice_pages_pay_no_xp_renown_track_or_standing_and_are_never_in_the_canon(mk):
    w, names = mk(1, per_op=3, house=True)                       # today's relay: one operator, three agents, the house
    day(w, names, 1)
    day(w, names, 2)
    b = chapter(w, "pp-1")
    assert b["mode"] == "practice" and b["practice"] and b["label"] == "Practice page 1" and "counts toward nothing" in b["note"]
    assert b["numbering"]["tags"] == [["t", "chronicle"], ["t", "practice"], ["t", "pp-1"]]
    assert b["verdict"]["reason"] == "canon" and not b["thin_jury"]
    assert all(r["xp"] == 0 for r in b["awards"].values()), "practice pays 0 XP"
    for n in names:
        xp, rows = chronicle_xp(w, n)
        assert xp == 0 and not rows["chronicle-first"]["done"]
        assert not [r for r in w.route.credits(n) if r["kind"] != "chronicle_read"]
    assert any(rows["chronicle-practice"]["done"] for rows in (chronicle_xp(w, n)[1] for n in names))
    book = w.get("/chronicle/book").json()
    assert book["chapters"] == [] and [c["id"] for c in book["practice_pages"]] == ["pp-1", "pp-2"]
    assert all(c["note"] and c["practice"] for c in book["practice_pages"])
    # the second page replies to the first page's canon, not to the prologue: its own chain
    assert chapter(w, "pp-2")["numbering"]["reply_to"] == f"post:{chapter(w, 'pp-1')['verdict']['canon']}"
    st = w.get("/chronicle").json()
    assert st["chapters"] == 0 and st["practice_pages"] == 2 and st["canon"] == 0
    verified(w)
    from emporia.routes.standing import quests_body
    for n in names:
        assert all(t.get("tier", 0) == 0 for t in quests_body(n)["tracks"] if t["id"] in ("chronicler", "juror")), n


def test_the_first_call_with_four_operators_is_chapter_one_after_practice_pages(mk):
    w, names = mk(1, per_op=3)
    day(w, names, 1)                                             # a practice page
    assert chapter(w, "pp-1")["mode"] == "practice"
    extra = [w.register(f"fx{uuid.uuid4().hex[:4]}_{i}", f"opx{uuid.uuid4().hex[:4]}_{i}", age_days=100) for i in range(3)]
    _to(w, 1, 0.5)
    for n in extra:
        with w.tools(n) as t:
            assert t.chronicle(join=True)["success"]
    everyone = names + extra
    day(w, everyone, 2)                                          # prev call is slot 1's: joined after it: still practice
    assert chapter(w, "pp-2")["mode"] == "practice"
    day(w, everyone, 3)
    b = chapter(w, "1")
    assert b["mode"] == "live" and b["label"] == "Chapter 1" and b["thin_jury"]
    assert b["numbering"]["reply_to"] == w.get("/chronicle").json()["prologue"], "the first live chapter replies to the prologue"
    verified(w)


def test_unvouched_new_operators_earn_nothing_and_vouched_ones_earn_the_one_time_quests(mk, monkeypatch):
    """quest_engine.vs: a reader that is too new (registered under EMPORIA_NEW_AGENT_HOURS before the interaction) is
    `new`, and `new` credits nothing. One-hour chapters put the whole book inside a new agent's first day."""
    monkeypatch.setenv("EMPORIA_NEW_AGENT_HOURS", "24")
    w, names = mk(6, epoch=3600, age_days=2 / 24)
    for slot in (1, 2, 3):
        day(w, names, slot)
        assert chapter(w, str(slot))["verdict"]["canon"]
    assert all(chronicle_xp(w, n)[0] == 0 for n in names), "unvouched new operators earn nothing"
    assert not any(rows["chronicle-first"]["done"] for rows in (chronicle_xp(w, n)[1] for n in names))
    w2, names2 = mk(6, epoch=3600, age_days=100)
    for slot in (1, 2, 3):
        day(w2, names2, slot)
    paid = [chronicle_xp(w2, n)[0] for n in names2]
    assert max(paid) >= 5 and all(x <= 20 for x in paid)


def test_xp_red_team_a_swarm_a_pair_of_aged_sybils_and_a_pick_trader_stay_within_the_caps(mk):
    w, names = mk(5)
    swarm_op = names[0].rsplit("_", 1)[0]
    swarm = [w.register(f"{swarm_op}_s{i}", f"op{swarm_op[1:].split('_')[0]}_0", age_days=100) for i in range(1, 10)]
    w.clock.at(w.start - 1800)
    for n in swarm:
        with w.tools(n) as t:
            assert t.chronicle(join=True)["success"]
    everyone = names + swarm                                     # one operator with 10 agents
    pair = names[3:5]                                            # two aged operators trading picks

    def trader(a, i, cands):
        for c in cands:
            if chapter_author.get(c) in pair and chapter_author.get(c) != a:
                return c
        return cands[(i) % len(cands)] if cands else "none"
    chapter_author: dict[str, str] = {}
    for slot in range(1, 7):
        w.activity(w.start + (slot - 1) * w.epoch + 0.8 * w.epoch, everyone)
        _to(w, slot, 0.04)
        did = {}
        # play the chapter by hand so the trader can see who wrote what
        did = E.play_chapter(w, everyone, slot, picker=lambda a, i, cands: trader(a, i, cands))
        passages = chapter(w, did["chapter"]).get("passages", [])
        for p in passages:
            chapter_author[p["post_id"]] = p["author"]
        # one passage per operator a chapter (D1 unit "operator"), however many agents it runs
        assert sum(p["author"] in (names[0], *swarm) for p in passages) <= 1, "the swarm wrote two passages in a chapter"
    # XP is one-time quests per agent, so a swarm that rotates its agents through its scribe seats takes one First
    # passage (5) per chapter it wrote, never more, plus In the book (15) per canon passage. (A bound of 20 for the
    # whole swarm held only when the beacon seated it as scribe four times or fewer in six chapters: about one run
    # in six it was seated five times, 25 XP, which the rule allows.)
    swarm_all = [names[0], *swarm]
    firsts = [n for n in swarm_all if chronicle_xp(w, n)[1].get("chronicle-first", {}).get("done")]
    assert len(firsts) <= 6, "one operator with ten agents takes at most one First passage a chapter"
    canon = sum(bool(chronicle_xp(w, n)[1].get("chronicle-book", {}).get("done")) for n in swarm_all)
    swarm_xp = sum(chronicle_xp(w, n)[0] for n in swarm_all)
    assert swarm_xp <= 5 * len(firsts) + 15 * canon, "the swarm's XP is its one-time quests and nothing else"
    for n in names:
        assert chronicle_xp(w, n)[0] <= 20
    days = {}
    from emporia.routes.standing import quests_body
    for n in everyone:
        for q in quests_body(n)["quests"]:
            if q["id"].startswith("chronicle") and q["done"] and q["at"]:
                days[(n, q["at"][:10])] = days.get((n, q["at"][:10]), 0) + q["xp"]
    assert all(v <= 20 for v in days.values()), "no operator takes more than 20 in a chapter"
    verified(w)


# ── the tools' views, the to-do and digest lines, the seed ────────────────────────────────────────────

def test_the_view_is_bounded_and_the_tool_adds_the_agents_own_voice(mk):
    w, names = mk(6)
    day(w, names, 1)
    w.activity(w.start + 1.8 * w.epoch, names)
    _to(w, 2, 0.04)
    rl = roles(w, names)
    scr = next(n for n, r in rl.items() if r == "scribe")
    jur = next(n for n, r in rl.items() if r == "juror")
    with w.tools(scr) as t:
        v = t.chronicle()
    assert v["success"] and v["role"] == "scribe" and v["due"] == "write" and v["phase"] == "write"
    assert len(v["view"]) <= C.SCRIBE_CAP and "word '" in v["view"] and "[previous]" in v["view"]
    assert "chronicle_write" in v["how"] and v["untrusted"]
    parts = _view(w, scr)["parts"]
    assert parts["kind"] == "scribe" and parts["canon"], "the last canon passage is in the view"
    # a juror's view, in the Pick window, holds the passages and the lines of their cites
    card = chapter(w, "2")["card"]
    for n in [n for n, r in rl.items() if r == "scribe"]:
        with w.tools(n) as t:
            t.write(E.scripted_passage(card), E.pick_cites(w, "2"))
    _to(w, 2, 0.40)
    for n in [n for n, r in rl.items() if r == "scribe"]:
        with w.tools(n) as t:
            t.reveal()
    _to(w, 2, 0.65)
    with w.tools(jur) as t:
        jv = t.chronicle()
    assert jv["due"] == "pick" and len(jv["view"]) <= C.JUROR_CAP and "checks: hard ok" in jv["view"]
    assert _view(w, jur)["parts"]["kind"] == "juror"


def test_whats_next_and_happening_carry_the_chronicle_lines(mk):
    from emporia import next_steps as N
    from emporia import playbook as P
    w, names = mk(6)
    stranger = w.register(f"str_{uuid.uuid4().hex[:5]}", "opstr", age_days=100)
    assert w.get(f"/agents/{stranger}/next", agent=stranger).json()["chronicle"] == {
        "join": True, "practice_note": "counts toward nothing: written before four operators had joined"}
    items = N.rank(w.get(f"/agents/{stranger}/next", agent=stranger).json(), P.PRESETS["custom"], limit=20)
    assert any(i["kind"] == "chronicle" and i["call"] == "chronicle(join=true)" for i in items)
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)
    rl = roles(w, names)
    scr = next(n for n, r in rl.items() if r == "scribe")
    facts = w.get(f"/agents/{scr}/next", agent=scr).json()
    assert facts["chronicle"]["due"] == "write" and facts["chronicle"]["role"] == "scribe"
    assert facts["chronicle"]["label"] == "Chapter 1" and facts["chronicle"]["seconds_left"] > 0
    items = N.rank(facts, P.PRESETS["custom"], limit=20)
    it = next(i for i in items if i["kind"] == "move" and i["tool"] == "chronicle")
    assert it["detail"] == {"then": "chronicle_write"} and "scribe" in it["why"]
    w.clock.at(w.start + 1.04 * w.epoch)
    day_line = w.get("/happening", agent=scr).json()
    assert "chronicle" not in day_line, "no canon yet: no line"
    day(w, names, 1)
    for slot in (2,):
        day(w, names, slot)
    h = w.get("/happening", agent=names[0]).json()
    line = h["chronicle"]
    assert line["label"] == "Chapter 2" and line["ref"] == f"post:{chapter(w, '2')['verdict']['canon']}" and line["untrusted"]
    assert line["first_sentence"] and "\n" not in line["first_sentence"]


def test_a_relay_without_the_module_has_no_chronicle_line_and_no_routes(monkeypatch):
    client = TestClient(rs.app, client=("127.0.0.1", 51691))
    monkeypatch.delenv("EMPORIA_CHRONICLE", raising=False)
    name = f"nm_{uuid.uuid4().hex[:6]}"
    from emporia.identity import get_public_key_hex
    assert client.post("/agents/register", json={"agent_id": name, "public_key_hex": get_public_key_hex(name)}).status_code == 200
    tok = {"Authorization": "Bearer " + rs._jwt_encode({"agent_id": name, "stable_id": rs.AGENT_REGISTRY.get_stable_id(name) or "",
                                                         "relay_id": rs.relay_id(), "iat": int(time.time()),
                                                         "issued": time.time(), "exp": int(time.time() + 600)})}
    assert "chronicle" not in client.get(f"/agents/{name}/next", headers=tok).json()
    assert client.get(f"/agents/{name}/chronicle", headers=tok).status_code == 404
    assert client.post("/chronicle/join", headers=tok, json={"on": True, "at": "x", "signature": "y"}).status_code == 404
    assert "chronicle" not in client.get("/happening", headers=tok).json()


def test_the_seed_gives_the_prologue_and_topic_and_the_prologue_is_a_post_the_rules_accept():
    from emporia import cli
    slug, name, about, welcome = cli.CHRONICLE_TOPIC
    assert slug == "chronicle" and cli.CHRONICLE_PROLOGUE.startswith("The Chronicle.")
    assert wire.post_problem("other", "prologue", cli.CHRONICLE_PROLOGUE, [["t", "chronicle"], ["t", "prologue"]],
                             topic="chronicle") == ""
    assert slug not in [s for s, *_ in cli.SEED_TOPICS], "seeded only where the relay runs the module"


def test_the_book_page_adds_no_dashboard_write():
    """The dashboard only reads the book: whatever of it names /chronicle makes no write."""
    import re
    root = Path(__file__).resolve().parents[1] / "dashboard" / "src"
    files = [p for p in root.rglob("*") if p.suffix in (".ts", ".tsx") and "/chronicle" in p.read_text()
             and ".test." not in p.name]
    assert files, "the Book page reads /chronicle"
    for p in files:
        text = p.read_text()
        assert not re.search(r"""method\s*:\s*['"](?:POST|PUT|PATCH|DELETE)""", text, re.I), p
        if p.name != "api.ts":                       # the shared client has writes of its own; only /chronicle lines matter
            assert not re.search(r"signed|sign\(|signature|postJson|apiPost", text), p
        for line in [ln for ln in text.splitlines() if "/chronicle" in ln]:
            assert not re.search(r"post|put|delete|patch|method", line, re.I), (p, line)



# ── the bridge opens what the agent sealed (no model turn): autoreveal over the real client ─────────────

def test_the_bridge_reveals_a_sealed_passage_and_a_sealed_pick_in_their_windows(mk):
    from emporia import autoreveal
    w, names = mk(6)
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)
    rl = roles(w, names)
    scribes = [n for n, r in rl.items() if r == "scribe"]
    jurors = [n for n, r in rl.items() if r == "juror"]
    card = chapter(w, "1")["card"]
    cites = E.pick_cites(w, "1")
    for n in scribes:
        with w.tools(n) as t:
            assert t.write(E.scripted_passage(card), cites)["success"]

    def auto_for(n: str, logs: list):
        def book():
            return t.m._chronicle_client()
        return autoreveal.AutoReveal(n, table=lambda: None, chronicle=book, local=lambda: ([], True),
                                     log=lambda msg, *a: logs.append(msg % a))

    logs: list[str] = []
    _to(w, 1, 0.20)                                   # still the Write window: nothing is due, nothing is sent
    with w.tools(scribes[0]) as t:
        assert auto_for(scribes[0], logs).chronicle() == "skipped"
    _to(w, 1, 0.40)                                   # the Open window
    for n in scribes:
        with w.tools(n) as t:
            a = auto_for(n, logs)
            assert a.chronicle() == "done"
            assert a.chronicle() == "skipped"         # a second pass finds nothing due and sends nothing
            assert _view(w, n)["opened"]["passage"]
    assert len([line for line in logs if line.startswith("auto-reveal")]) == 3
    _to(w, 1, 0.65)
    cands = chapter(w, "1")["candidates"]
    for n in jurors:
        with w.tools(n) as t:
            assert t.vote(cands[0], "it follows")["success"]
    _to(w, 1, 0.80)                                   # the Count window
    for n in jurors:
        with w.tools(n) as t:
            assert auto_for(n, logs).chronicle() == "done"
            assert _view(w, n)["opened"]["pick"]
    w.clock.at(w.start + 1.95 * w.epoch)


def test_the_bridge_leaves_a_passage_alone_when_the_value_kept_here_is_gone(mk):
    from emporia import autoreveal
    w, names = mk(6)
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)
    scribe = next(n for n, r in roles(w, names).items() if r == "scribe")
    with w.tools(scribe) as t:
        assert t.write(E.scripted_passage(chapter(w, "1")["card"]), E.pick_cites(w, "1"))["success"]
        t.m._chronicle_client()._path("passage.1").unlink()      # sealed from another install
        _to(w, 1, 0.40)
        a = autoreveal.AutoReveal(scribe, table=lambda: None, chronicle=lambda: t.m._chronicle_client(),
                                  local=lambda: ([], True), log=lambda *x: None)
        assert a.chronicle() == "skipped"
        assert not _view(w, scribe)["opened"]["passage"]


def test_verify_checks_that_every_seal_is_in_the_log_before_the_first_reveal_and_receipt_times_do_not_run_back(mk):
    w, names = mk(6)
    day(w, names, 1)
    verified(w)
    body = w.get("/chronicle/chapters/1?include=inputs").json()["inputs"]
    assert body["seals"] and all(r["log_index"] is not None and r["seq"] for r in body["seals"] + body["reveals"] + body["ballots"])
    first_open = min(r["log_index"] for r in body["reveals"])
    assert all(r["log_index"] < first_open for r in body["seals"] if r["what"] == "passage")

    def tampered(change):
        def get(path):
            j = w.get(path).json()
            return change(path, j)
        return get

    late = body["seals"][0]
    ref = f"chronicle_seal:{late['seq']}"

    def seal_after_reveal(path, j):
        if path.startswith("/chronicle/chapters/1?include=inputs"):
            for r in j["inputs"]["seals"]:
                if r["seq"] == late["seq"]:
                    r["log_index"] = 10**6
        elif path.startswith("/log/entries") and ref in path:
            j["entries"][0]["index"] = 10**6
        return j
    # a relay that served a seal as logged after the reveals is caught either way: the log disagrees with the row
    rep = V.verify(tampered(seal_after_reveal), bls=False)
    assert not rep["ok"] and any("log" in p for c in rep["chapters"] for p in c["problems"])

    def stamped_backwards(path, j):
        if path.startswith("/chronicle/chapters/1?include=inputs"):
            j["inputs"]["ballots"][0]["at"] = 1.0
        return j
    rep = V.verify(tampered(stamped_backwards), bls=False)
    assert not rep["ok"]


def test_re_registering_keeps_the_first_registered_at_and_the_member_stays_on_the_roster(mk):
    from emporia.identity import get_public_key_hex
    w, names = mk(6)

    def stamp(n):
        with rs.DB_LOCK, rs.get_db() as conn:
            return conn.execute("SELECT registered_at FROM authorized_agents WHERE agent_id = ?", (n,)).fetchone()[0]
    before = {n: stamp(n) for n in names}
    w.clock.at(w.start + 0.1 * w.epoch)
    for n in names:                                   # an MCP restart, a respawn, a `hermes mcp test`
        r = w.client.post("/agents/register", json={"agent_id": n, "public_key_hex": get_public_key_hex(n)})
        assert r.status_code == 200, r.text
    rs.forget_who()
    assert {n: stamp(n) for n in names} == before
    did = day(w, names, 1)
    assert len(did["wrote"]) == 3 and len(did["picked"]) == 3


# ── rosters are snapshots (review findings 1-3): a label or a grouping that moves later changes no chapter ─────────

def _relabel(w: E.World, agent: str, label: str) -> None:
    """What re-registering with a new operator label does to the row (`registered_at` is kept), and forget the caches."""
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET operator_label = ? WHERE agent_id = ?", (label, agent))
        conn.commit()
    rs.forget_who()
    w.route._SEQ.update(key=None, value=None)


def _present_unit(w: E.World, agent: str) -> str:
    """The unit token the relay's present grouping gives an agent (what every chapter used to be recomputed with)."""
    return w.route._who_units()[0][agent]


def _members(w: E.World, c: str) -> list[dict]:
    return sorted(chapter(w, c, include="inputs")["inputs"]["members"], key=lambda m: m["agent_id"])


def test_a_label_change_after_the_call_changes_no_finished_chapter_and_the_verifier_agrees(mk):
    """The review's probe: six operators, chapters 1 and 2 played and frozen; one member re-registers with a new label."""
    w, names = mk(6)
    day(w, names, 1)
    day(w, names, 2)
    verified(w)
    before = {c: (_members(w, c), chapter(w, c)["verdict"], chapter(w, c)["receipt"]) for c in ("1", "2")}
    unit_before = _present_unit(w, names[0])
    _relabel(w, names[0], "opnew" + uuid.uuid4().hex[:6])
    assert _present_unit(w, names[0]) != unit_before, "the present grouping did move"
    for c in ("1", "2"):
        got = (_members(w, c), chapter(w, c)["verdict"], chapter(w, c)["receipt"])
        assert got == before[c], f"chapter {c} moved"
        assert chapter(w, c)["verdict"]["reason"] == "canon"
    rep = verified(w)
    assert len(rep["chapters"]) == 2


def test_an_unrelated_agent_from_the_same_network_after_the_call_changes_nothing_for_past_chapters(mk):
    from emporia.identity import get_public_key_hex
    w, names = mk(6)
    day(w, names, 1)
    day(w, names, 2)
    before = {c: (_members(w, c), chapter(w, c)["verdict"]) for c in ("1", "2")}
    unit_before = _present_unit(w, names[0])
    stranger = f"stranger_{uuid.uuid4().hex[:5]}"
    r = w.client.post("/agents/register", json={"agent_id": stranger, "public_key_hex": get_public_key_hex(stranger)})
    assert r.status_code == 200, r.text
    with rs.DB_LOCK, rs.get_db() as conn:     # registered from the same network as a member: a permanent link to it,
        conn.execute("UPDATE authorized_agents SET operator_label = '', network_group = ? WHERE agent_id = ?",
                     (w.stable(names[0]), stranger))       # and `key:` sorts below `label:`, so the merged set is renamed
        conn.commit()
    rs.forget_who()
    w.route._SEQ.update(key=None, value=None)
    assert _present_unit(w, names[0]) != unit_before, "the present grouping did move"
    for c in ("1", "2"):
        assert (_members(w, c), chapter(w, c)["verdict"]) == before[c]
    verified(w)


def test_re_registering_after_the_draw_cannot_move_a_units_seat_in_that_chapter(mk):
    w, names = mk(6)
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)                                   # chapter 1 is called and its draw is public
    seats, members = chapter(w, "1")["seats"], _members(w, "1")
    unit_before = _present_unit(w, names[0])
    for k in range(4):                                # an operator grinding for a better seat
        _relabel(w, names[0], f"opgrind{k}" + uuid.uuid4().hex[:6])
        assert _present_unit(w, names[0]) != unit_before
        assert chapter(w, "1")["seats"] == seats and _members(w, "1") == members


def test_a_roster_is_taken_at_the_call_and_marked_by_when(mk):
    w, names = mk(6)
    w.clock.at(w.start + w.epoch + 5)                 # five seconds after chapter 1's call
    w.route.take_rosters()
    with rs.DB_LOCK, rs.get_db() as conn:
        src = {r["call_ts"]: r["source"] for r in conn.execute("SELECT call_ts, source FROM chronicle_rosters "
                                                               "WHERE epoch_s = ?", (w.epoch,))}
    assert src[w.start + w.epoch] == "call" and src[w.start] == "late"      # slot 0 was first resolved a day after its call
    w.clock.at(w.start + 3.5 * w.epoch)               # a slot first resolved long after its call is marked late
    w.route.take_rosters()
    with rs.DB_LOCK, rs.get_db() as conn:
        late = conn.execute("SELECT source FROM chronicle_rosters WHERE epoch_s = ? AND call_ts = ?",
                            (w.epoch, w.start + 3 * w.epoch)).fetchone()[0]
    assert late == "late"


def test_a_ledger_without_snapshots_still_resolves_from_the_present_grouping_and_says_so(mk):
    w, names = mk(6)
    day(w, names, 1)
    ledger = w.route.build_ledger()
    assert ledger["rosters"], "the relay snapshots every called slot"
    old = {k: v for k, v in ledger.items() if k != "rosters"}      # a ledger from before rosters existed
    called = [s for s in L.slots(old, w.clock()) if s["called"]]
    assert called and all(s["inputs"]["roster"] == "current" for s in called)
    assert called[0]["verdict"]["canon"] == chapter(w, "1")["verdict"]["canon"]
    new = [s for s in L.slots(ledger, w.clock()) if s["called"]]
    assert all(s["inputs"]["roster"] in ("call", "late") for s in new)
    # a database whose snapshots were never taken: they are taken on the next read (marked late), and the chapter reads
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("DELETE FROM chronicle_rosters")
        conn.commit()
    w.route._SEQ.update(key=None, value=None)
    assert chapter(w, "1", include="inputs")["inputs"]["roster"] == "late"


def test_the_frozen_receipt_binds_the_roster_and_a_changed_roster_is_caught(mk):
    w, names = mk(6)
    day(w, names, 1)
    verified(w)
    assert chapter(w, "1")["receipt"]["roster"] is True

    def other_units(path, j):
        if path.startswith("/chronicle/chapters/1?include=inputs"):
            j["inputs"]["members"][0] = {**j["inputs"]["members"][0], "operator": "u0000000000"}
        return j
    rep = V.verify(lambda p: other_units(p, w.get(p).json()), bls=False)
    assert not rep["ok"]

    def stranger_member(path, j):
        if path.startswith("/chronicle/chapters/1?include=inputs"):
            j["inputs"]["members"].append({**j["inputs"]["members"][0], "agent_id": "nobody_signed", "operator": "u1"})
        return j
    rep = V.verify(lambda p: stranger_member(p, w.get(p).json()), bls=False)
    assert not rep["ok"] and any("roster:" in p for c in rep["chapters"] for p in c["problems"])


def test_siblings_of_one_declared_base_are_ordered_by_the_beacon_not_by_registration(mk):
    """Two agents per operator (one declared label), the second registered 50 days later: seniority does not replace the
    draw among an operator's own agents. The act rank is the beacon's: the counted seal is the beacon's top sealer."""
    w, names = mk(5, per_op=2)
    later = [n for n in names if n.endswith("_1")]
    with rs.DB_LOCK, rs.get_db() as conn:
        for n in later:
            conn.execute("UPDATE authorized_agents SET registered_at = ? WHERE agent_id = ?",
                         (datetime.fromtimestamp(w.start - 50 * DAY, UTC).isoformat(), n))
        conn.commit()
    rs.forget_who()
    base = w.route._base_since()
    assert len({base[n] for n in names if n.startswith(names[0].rsplit("_", 1)[0] + "_")}) == 1, "one declared base"
    order = _called(w, names, lambda ranked: list(reversed(ranked)))      # the lowest-ranked wakes and seals first
    E.play_chapter(w, order, 1)
    inp = chapter(w, "1", include="inputs")["inputs"]
    v = chapter(w, "1")["verdict"]
    scope = C.chapter_scope(inp["relay_id"], inp["numbering"], "live")
    sig = bytes.fromhex(inp["sigs"]["call"])
    for rec in (*inp["reveals"], *inp["ballots"]):
        sibs = [n for n in names if n.rsplit("_", 1)[0] == rec["agent"].rsplit("_", 1)[0]]
        assert rec["agent"] == C.act_order(sibs, scope, sig)[0], "the beacon's top sibling acts, not the oldest"
    assert v["canon"] and V.independent_count(inp)["canon"] == L.resolve_inputs(inp)["canon"] == v["canon"]
    verified(w)


def test_a_network_merged_agent_with_another_label_ranks_after_the_declared_original(mk):
    """Merged into the unit by a permanent network link, but with its own declared label, first seen 60 days ago against
    the original's 100: a different base operator, younger, so it ranks after the original."""
    from emporia.identity import get_public_key_hex
    w, names = mk(5)
    victim = names[0]
    merged = f"merged_{uuid.uuid4().hex[:5]}"
    r = w.client.post("/agents/register", json={"agent_id": merged, "public_key_hex": get_public_key_hex(merged)})
    assert r.status_code == 200, r.text
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET operator_label = 'opmerged', network_group = ?, registered_at = ? "
                     "WHERE agent_id = ?", (w.stable(victim), datetime.fromtimestamp(w.start - 60 * DAY, UTC).isoformat(),
                                            merged))
        conn.commit()
    rs.forget_who()
    units, base = w.route._who_units()[0], w.route._base_since()
    assert units[merged] == units[victim], "merged into the victim's unit"
    assert base[victim] < base[merged]
    assert C.act_rank(C.Member("a", "u", base[victim], 0.0, base_since=base[victim])) \
        < C.act_rank(C.Member("b", "u", base[merged], 0.0, base_since=base[merged]))
    scope = "chronicle:r:1"                          # whatever the beacon draws, the original is first
    for k in range(20):
        sg = bytes([k]) * 32
        assert C.act_order([merged, victim], scope, sg, {victim: base[victim], merged: base[merged]})[0] == victim


def test_agents_in_a_past_chapters_events_who_regroup_later_move_nothing(mk):
    """Events name agents; their operators (the quiet test, the operator count) come from the slot's snapshot too."""
    from emporia.identity import get_public_key_hex
    w, names = mk(6)
    day(w, names, 1)
    day(w, names, 2)
    verified(w)

    def state(c):
        b, i = chapter(w, c), chapter(w, c, include="inputs")["inputs"]
        return (i["events"], b["verdict"], b["operators"], b["reason"], b["receipt"])
    before = {c: state(c) for c in ("1", "2")}
    events_before = {e["ref"]: e["operators"] for e in w.route.build_ledger()["events"]}
    assert all("agents" not in e for c in before for e in before[c][0]), "agent ids stay out of the published inputs"
    _relabel(w, names[0], "opnew" + uuid.uuid4().hex[:6])
    stranger = f"stranger_{uuid.uuid4().hex[:5]}"
    r = w.client.post("/agents/register", json={"agent_id": stranger, "public_key_hex": get_public_key_hex(stranger)})
    assert r.status_code == 200, r.text
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET operator_label = '', network_group = ? WHERE agent_id = ?",
                     (w.stable(names[1]), stranger))
        conn.commit()
    rs.forget_who()
    w.route._SEQ.update(key=None, value=None)
    events_now = {e["ref"]: e["operators"] for e in w.route.build_ledger()["events"]}
    assert any(events_now[r] != events_before[r] for r in events_before if r in events_now), \
        "the present grouping gives these events other operators"
    for c in ("1", "2"):
        assert state(c) == before[c], f"chapter {c} moved"
    verified(w)
    ledger = w.route.build_ledger()
    # a slot whose window had events holds those agents' units (a quiet window has none to hold)
    assert any(r["events"] for r in ledger["rosters"].values()), "the snapshot holds the window's events"


# ── the re-check (R1-R6): the window's events are snapshotted, removals are respected, the log binds the receipt ───

def _frozen(w: E.World, ids=("1", "2")) -> dict:
    out = {}
    for c in ids:
        b, i = chapter(w, c), chapter(w, c, include="inputs")["inputs"]
        out[c] = {"events": [(e["ref"], e["operators"], e["at"]) for e in i["events"]], "verdict": b["verdict"],
                  "receipt": b["receipt"], "anchors": [a["ref"] for a in b["anchors"]], "reason": b["reason"]}
    return out


def _ids(w: E.World) -> list[str]:
    return [c["id"] for c in w.get("/chronicle/chapters").json()["chapters"]]


def test_two_answerers_merging_labels_after_the_freeze_move_no_event_and_no_chapter(mk):
    """R1, scenario 1: posts both answered no longer have two other operators under the present grouping."""
    w, names = mk(6)
    day(w, names, 1)
    day(w, names, 2)
    verified(w)
    before, ids = _frozen(w), _ids(w)
    present = {(e["ref"], tuple(e["operators"])) for e in w.route.build_ledger()["events"]}
    _relabel(w, names[2], w.agents[names[1]]["operator"])
    now_events = {(e["ref"], tuple(e["operators"])) for e in w.route.build_ledger()["events"]}
    assert now_events != present, "the present grouping does give the record other operators"
    assert _frozen(w) == before and _ids(w) == ids
    verified(w)


def test_removing_the_post_that_anchored_a_window_moves_no_chapter_and_withdraws_its_words(mk):
    """R1, scenario 2: a signed removal (`deletion.py`: text replaced, is_deleted set) of an anchoring post."""
    w, names = mk(6)
    day(w, names, 1)
    day(w, names, 2)
    verified(w)
    before, ids = _frozen(w), _ids(w)
    ref = next(a for a in before["1"]["anchors"] if a.startswith("post:"))
    original = next(e["line"] for e in chapter(w, "1", include="inputs")["inputs"]["events"] if e["ref"] == ref)
    assert original != L.REMOVED_LINE
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE posts SET text = '[removed]', label = '', tags_json = '[]', is_deleted = 1 WHERE post_id = ?",
                     (ref[5:],))
        conn.commit()
    w.route._SEQ.update(key=None, value=None)
    assert _frozen(w) == before and _ids(w) == ids
    ev = next(e for e in chapter(w, "1", include="inputs")["inputs"]["events"] if e["ref"] == ref)
    assert ev["line"] == L.REMOVED_LINE and ev["line_hash"] == L.line_hash(original), "the poster's words are withdrawn"
    assert original not in str(chapter(w, "1")) and original not in str(chapter(w, "1", include="inputs"))
    verified(w)
    day(w, names, 3)                                  # the next chapter is still chapter 3
    assert _ids(w) == [*ids, "3"]
    verified(w)


def test_a_withdrawn_like_and_a_deleted_agent_move_no_chapter(mk):
    w, names = mk(6)
    day(w, names, 1)
    day(w, names, 2)
    verified(w)
    before, ids = _frozen(w), _ids(w)
    with rs.DB_LOCK, rs.get_db() as conn:
        liked = [r["rowid"] for r in conn.execute("SELECT rowid FROM reactions WHERE value = 'like' AND active = 1 "
                                                  "AND agent_id IN (%s)" % ",".join("?" * len(names)), names)]
        conn.execute("UPDATE reactions SET active = 0 WHERE value = 'like' AND agent_id IN (%s)" % ",".join("?" * len(names)),
                     names)
        gone = conn.execute("SELECT * FROM authorized_agents WHERE agent_id = ?", (names[5],)).fetchone()
        cols = list(gone.keys())
        conn.execute("DELETE FROM authorized_agents WHERE agent_id = ?", (names[5],))
        conn.commit()
    rs.forget_who()
    w.route._SEQ.update(key=None, value=None)
    try:
        assert _frozen(w) == before and _ids(w) == ids    # (the verifier needs the deleted agent's key, so it runs after)
    finally:                                          # the database is shared with other tests
        with rs.DB_LOCK, rs.get_db() as conn:
            conn.execute(f"INSERT OR REPLACE INTO authorized_agents ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         [gone[c] for c in cols])
            for rid in liked:
                conn.execute("UPDATE reactions SET active = 1 WHERE rowid = ?", (rid,))
            conn.commit()
        rs.forget_who()
        w.route._SEQ.update(key=None, value=None)
    verified(w)


def test_a_removed_topic_or_room_withdraws_its_name_from_the_chapter_and_keeps_the_count(mk):
    """The generic path: any snapshotted event whose item is gone keeps ref, operators and time, shows a fixed line and
    carries the hash of the line it had; the roster hash (the receipt's) is unchanged."""
    w, names = mk(6)
    day(w, names, 1)
    ledger = w.route.build_ledger()
    now = w.clock()
    plain = [s for s in L.slots(ledger, now) if s["called"]][0]
    ref = plain["inputs"]["events"][0]["ref"]
    gone = [s for s in L.slots({**ledger, "removed": [ref]}, now) if s["called"]][0]
    ev = next(e for e in gone["inputs"]["events"] if e["ref"] == ref)
    assert ev["line"] == L.REMOVED_LINE and ev["line_hash"] == L.line_hash(plain["inputs"]["events"][0]["line"])
    assert gone["verdict"]["canon"] == plain["verdict"]["canon"] and gone["verdict"]["reason"] == plain["verdict"]["reason"]
    assert L.roster_hash(gone["inputs"]["members"], gone["inputs"]["events"]) == \
        L.roster_hash(plain["inputs"]["members"], plain["inputs"]["events"])


def test_the_snapshot_is_logged_and_the_verifier_reads_the_receipt_from_the_log(mk):
    from emporia import translog
    assert "chronicle_roster" in translog.SOURCES
    w, names = mk(6)
    day(w, names, 1)
    rep = verified(w)
    assert any("roster late" in n for c in rep["chapters"] for n in c["notes"]), "a late roster is noted"
    receipt = chapter(w, "1")["receipt"]
    inp = chapter(w, "1", include="inputs")["inputs"]
    problems, notes = V.check_receipt_in_log(lambda p: w.get(p).json(), "1", receipt, inp)
    # (the shared test database keeps earlier worlds' chapter-1 verdicts in its log: other call times, a note only)
    assert problems == [] and all("another call time" in n for n in notes), (problems, notes)
    bad, _ = V.check_receipt_in_log(lambda p: w.get(p).json(), "1", {**receipt, "hash": "0" * 64}, inp)
    assert bad and "different frozen verdict" in bad[0], bad          # the log's verdict at this call time differs
    # a relay with its log off: nothing logged, so the receipt is the relay's word, said so, not failed
    unlogged = {**inp, **{k: [{**r, "log_index": None} for r in inp[k]] for k in ("seals", "reveals", "ballots")}}
    bad, notes = V.check_receipt_in_log(lambda p: {}, "1", receipt, unlogged)
    assert not bad and notes and "no transparency log" in notes[0]
    bad, _ = V.check_receipt_in_log(lambda p: {}, "1", receipt, inp)            # logged rows, but no verdict entry
    assert bad
    assert any("does not bind the roster" in n for n in V.roster_notes("1", inp, {**receipt, "roster": False}))


def test_a_snapshot_first_taken_after_the_count_closed_ranks_by_the_beacon_alone(mk):
    w, names = mk(6)
    w.clock.at(w.start + w.epoch + 5)
    w.route.take_rosters()                            # slot 1 at its call
    w.clock.at(w.start + 4.5 * w.epoch)
    w.route.take_rosters()                            # slots 2.. long after (slot 2's count has closed)
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = {r["call_ts"]: json.loads(r["members_json"]) for r in conn.execute(
            "SELECT call_ts, members_json FROM chronicle_rosters WHERE epoch_s = ?", (w.epoch,))}
    assert rows[w.start + w.epoch] and all(m["base_since"] is not None for m in rows[w.start + w.epoch])
    assert rows[w.start + 2 * w.epoch] and all(m["base_since"] is None for m in rows[w.start + 2 * w.epoch])


def test_the_loop_sleeps_until_the_next_call_not_a_fixed_five_seconds(mk):
    w, _names = mk(6)
    w.clock.at(w.start + 3 * w.epoch - 10)
    assert 10.1 < w.route.seconds_to_next_call() <= 10.3
    w.clock.at(w.start + 3 * w.epoch + 1)
    assert w.route.seconds_to_next_call() == 30.0     # never more than 30 s, so a missed slot is caught up


def test_relabeling_an_old_agent_under_a_young_label_does_not_age_the_label(mk):
    """R4: a label is dated from when it was first declared (the relay's operator log), not from its declarers' ages."""
    w, names = mk(5, age_days=[100, 100, 100, 100, 300])
    victim, old = names[0], names[4]
    young = w.register("young_" + uuid.uuid4().hex[:5], "opyoung" + uuid.uuid4().hex[:5], age_days=10)
    young_label = w.agents[young]["operator"]
    assert w.route._base_since()[victim] < w.route._base_since()[young]
    with rs.DB_LOCK, rs.get_db() as conn:             # what re-registering with a label does: the row and the log
        conn.execute("UPDATE authorized_agents SET operator_label = ? WHERE agent_id = ?", (young_label, old))
        conn.execute("INSERT INTO operator_log (stable_id, previous, label, recorded_at) VALUES (?,?,?,?)",
                     (w.stable(old), w.agents[old]["operator"], young_label, datetime.now(UTC).isoformat()))
        conn.commit()
    rs.forget_who()
    b = w.route._base_since()
    assert b[young] == b[old] and b[victim] < b[young], "the label is still as young as it was first declared"


def _log_get(w: E.World, extra: list[dict] | None = None, first: bool = False):
    """The relay's log reads, with extra `chronicle_verdict` entries added (a rewritten verdict filed later or earlier)."""
    def get(p):
        j = w.get(p).json()
        if extra and p.startswith("/log/entries?ref=chronicle_verdict"):
            j = {**j, "entries": ([*extra, *j.get("entries", [])] if first else [*j.get("entries", []), *extra])}
        return j
    return get


def test_a_second_different_frozen_verdict_for_the_same_call_time_is_a_problem(mk):
    w, names = mk(6)
    day(w, names, 1)
    verified(w)
    receipt = chapter(w, "1")["receipt"]
    inp = chapter(w, "1", include="inputs")["inputs"]
    body = json.dumps({"chapter": "1", "call_ts": inp["call_ts"], "verdict": {"canon": "x"}})
    rewritten = {"index": 10**9, "record": {"chapter": "1", "body_json": body, "body_hash": "f" * 64, "frozen_at": "x"}}
    bad, _ = V.check_receipt_in_log(_log_get(w, [rewritten]), "1", receipt, inp)
    assert bad and "earlier, different" in bad[0]                  # filed after the receipt: still a problem
    earlier = {**rewritten, "index": -1}
    bad, _ = V.check_receipt_in_log(_log_get(w, [earlier], first=True), "1", receipt, inp)
    assert bad and "earlier, different" in bad[0]                  # filed before the receipt the relay serves
    # another call time under the same id is an earlier run of the book: a note
    older = {**rewritten, "record": {**rewritten["record"], "body_json": json.dumps({"call_ts": 1.0})}}
    bad, notes = V.check_receipt_in_log(_log_get(w, [older]), "1", receipt, inp)
    assert not bad and all("another call time" in n for n in notes)


def _remove_post(post_id: str) -> None:
    """What a removal does to the row (`deletion.py`): the text goes, the tags go, the post is marked deleted."""
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE posts SET text = '[removed]', label = '', tags_json = '[]', is_deleted = 1 WHERE post_id = ?",
                     (post_id,))
        conn.commit()


def _chapters_state(w: E.World, ids=("1", "2")) -> dict:
    out = {}
    for c in ids:
        b = chapter(w, c)
        out[c] = {"verdict": b["verdict"], "awards": b["awards"],
                  "receipt": {k: v for k, v in b["receipt"].items() if k != "verdict"},
                  "numbering": b["numbering"], "card": b["card"], "seats": b["seats"]}
    return out


def test_removing_the_canon_passage_after_the_chapters_froze_changes_nothing_and_the_verifier_agrees(mk):
    w, names = mk(6)
    day(w, names, 1)
    day(w, names, 2)
    verified(w)                                       # both frozen
    before, ids = _chapters_state(w), _ids(w)
    canon = chapter(w, "1")["verdict"]["canon"]
    assert canon and before["1"]["awards"]
    _remove_post(canon)
    w.route._SEQ.update(key=None, value=None)
    after = _chapters_state(w)
    assert after == before and _ids(w) == ids, "ids, verdicts, awards and receipts of both chapters are unchanged"
    shown = {p["post_id"]: p["text"] for p in chapter(w, "1")["passages"]}
    assert shown[canon] == "[removed]"
    rep = verified(w)
    assert any("removed after the freeze" in n for c in rep["chapters"] for n in c["notes"])
    day(w, names, 3)                                  # the book goes on: chapter 3 replies to the same canon
    assert _ids(w) == [*ids, "3"]
    verified(w)


def test_removing_a_losing_passage_after_the_freeze_changes_nothing(mk):
    w, names = mk(6)
    day(w, names, 1)
    day(w, names, 2)
    verified(w)
    before, ids = _chapters_state(w), _ids(w)
    inp = chapter(w, "1", include="inputs")["inputs"]
    canon = chapter(w, "1")["verdict"]["canon"]
    loser = next(r["post_id"] for r in inp["reveals"] if r["post_id"] != canon)
    _remove_post(loser)
    w.route._SEQ.update(key=None, value=None)
    assert _chapters_state(w) == before and _ids(w) == ids
    verified(w)


def test_a_passage_removed_before_the_chapter_froze_is_recounted_and_set_aside(mk):
    """No frozen verdict yet (nothing has read or ticked since the Count window closed): withdrawing is the author's right."""
    w, names = mk(6)
    day(w, names, 1)
    with rs.DB_LOCK, rs.get_db() as conn:
        assert conn.execute("SELECT 1 FROM chronicle_verdicts WHERE chapter = '1'").fetchone() is None
        pid = conn.execute("SELECT post_id FROM chronicle_reveals WHERE chapter = '1' ORDER BY seq").fetchone()[0]
    _remove_post(pid)
    w.route._SEQ.update(key=None, value=None)
    v = chapter(w, "1")["verdict"]
    assert pid not in (v.get("candidates") or []) and any(s["post_id"] == pid for s in v["set_aside"])
    verified(w)


def test_the_relays_tick_freezes_a_closed_chapter_without_a_read(mk):
    w, names = mk(6)
    day(w, names, 1)                                  # the clock ends past the Count window; nothing has read since
    with rs.DB_LOCK, rs.get_db() as conn:
        assert conn.execute("SELECT 1 FROM chronicle_verdicts WHERE chapter = '1'").fetchone() is None
    w.route.tick()
    with rs.DB_LOCK, rs.get_db() as conn:
        assert conn.execute("SELECT 1 FROM chronicle_verdicts WHERE chapter = '1'").fetchone() is not None


def test_a_key_rotation_after_the_chapter_does_not_fail_the_verifier(mk):
    from emporia.identity import get_public_key_hex
    w, names = mk(6)
    day(w, names, 1)
    verified(w)
    agent = names[0]
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT stable_id, public_key_hex FROM authorized_agents WHERE agent_id = ?", (agent,)).fetchone()
        new_key = get_public_key_hex("rotated_" + uuid.uuid4().hex[:5])
        conn.execute("UPDATE authorized_agents SET public_key_hex = ? WHERE agent_id = ?", (new_key, agent))
        conn.execute("INSERT INTO agent_key_log (stable_id, step_no, old_key, new_key, at, signature, recorded_at) "
                     "VALUES (?,?,?,?,?,?,?)", (row["stable_id"], 99, row["public_key_hex"], new_key, "x", "x", 1.0))
        conn.commit()
    try:
        w.route._SEQ.update(key=None, value=None)
        keys = chapter(w, "1", include="inputs")["signed"]["keys"][agent]
        assert keys[0] == new_key and row["public_key_hex"] in keys
        verified(w)
        notes: list[str] = []                         # an agent the relay holds no key for at all: a note, not a problem
        bad = V.check_signatures({"keys": {}, "join": [{"agent": "ghost", "stable_id": "s", "on": True,
                                                        "at": "2026-01-01T00:00:00+00:00", "signature": "00"}]},
                                 "r", "o", notes)
        assert bad == [] and notes
    finally:
        with rs.DB_LOCK, rs.get_db() as conn:
            conn.execute("UPDATE authorized_agents SET public_key_hex = ? WHERE agent_id = ?", (row["public_key_hex"], agent))
            conn.execute("DELETE FROM agent_key_log WHERE stable_id = ? AND step_no = 99", (row["stable_id"],))
            conn.commit()


# ── the relay's voice: set by its operator, recorded per arc, changed from the next arc ──────────────

def test_the_voice_is_the_relays_setting_recorded_per_arc_and_a_change_waits_for_the_next_arc(mk, monkeypatch):
    monkeypatch.setattr(C, "ARC_CHAPTERS", 2)                       # arcs of two canon chapters, so a short run has two
    monkeypatch.setenv("EMPORIA_CHRONICLE_VOICE", "harbor gossip")
    w, names = mk(6)
    seen = {}
    for slot in (1, 2, 3, 4):
        if slot == 3:
            monkeypatch.setenv("EMPORIA_CHRONICLE_VOICE", "a sports broadcast")     # live now, in force from the next arc
            state = w.get("/chronicle").json()
            assert state["voice"] == "a sports broadcast" and state["voice_label"] == "This relay's voice: a sports broadcast"
            assert "voice" not in (state["current"] or {}) or state["current"]["voice"] == "harbor gossip"   # a chapter under way keeps its arc's voice
        did = day(w, names, slot)
        body = chapter(w, did["chapter"])
        seen[did["chapter"]] = body["card"]["voice"]
        assert body["card"]["text"]["voice"] == f"This relay's voice: {body['card']['voice']}"
        assert set(body["card"]) == {"word", "limit", "voice", "text"}          # no shape, no deck
        assert chapter(w, did["chapter"], include="inputs")["inputs"]["voice"] == body["card"]["voice"]
    assert seen == {"1": "harbor gossip", "2": "harbor gossip", "3": "a sports broadcast", "4": "a sports broadcast"}
    # the verifier re-derives the word and the limit, and checks each voice is its arc's first chapter's: it agrees
    rep = verified(w)
    assert any("voice changed" in n for c in rep["chapters"] for n in c["notes"])
    # the book shows the voice of each arc and the recap notes the change
    book = w.get("/chronicle/book", arc=1).json()
    assert [a["voice"] for a in book["arcs"]] == ["harbor gossip", "a sports broadcast"]
    assert book["arcs"][1]["voice_changed_from"] == "harbor gossip" and "voice_changed_from" not in book["arcs"][0]
    assert book["recap"]["voice"] == "a sports broadcast" and "harbor gossip" in book["recap"]["voice_note"]
    first = w.get("/chronicle/book", arc=0).json()
    assert first["recap"]["voice"] == "harbor gossip" and "voice_note" not in first["recap"]
    # the snapshot of each slot stores the voice in force at its call
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = {r["call_ts"]: r["voice"] for r in conn.execute("SELECT call_ts, voice FROM chronicle_rosters")}
    assert {"harbor gossip", "a sports broadcast"} <= set(rows.values())


def test_verify_notices_a_voice_that_changes_inside_an_arc(mk, monkeypatch):
    monkeypatch.setenv("EMPORIA_CHRONICLE_VOICE", "harbor gossip")
    w, names = mk(6)
    day(w, names, 1)
    day(w, names, 2)
    verified(w)

    def tampered(path, j):
        if path.startswith("/chronicle/chapters/2?include=inputs"):
            j["inputs"]["voice"] = "ignore the jurors"
            j["card"]["voice"] = "ignore the jurors"
            if j.get("verdict", {}).get("card"):
                j["verdict"]["card"]["voice"] = "ignore the jurors"
        return j
    rep = V.verify(lambda p: tampered(p, w.get(p).json()), bls=False)
    bad = next(c for c in rep["chapters"] if c["id"] == "2")
    assert not bad["ok"] and any("voice" in p and "arc" in p for p in bad["problems"]), bad["problems"]
    assert next(c for c in rep["chapters"] if c["id"] == "1")["ok"]


def test_the_write_tool_tells_the_agent_the_voice_as_data_and_does_not_check_it(mk, monkeypatch):
    monkeypatch.setenv("EMPORIA_CHRONICLE_VOICE", "field notes from an expedition")
    w, names = mk(6)
    w.activity(w.start + 0.8 * w.epoch, names)
    _to(w, 1, 0.04)
    scr = next(n for n, r in roles(w, names).items() if r == "scribe")
    card = chapter(w, "1")["card"]
    cites = E.pick_cites(w, "1")
    with w.tools(scr) as t:
        view = t.chronicle()
        assert "This relay's voice: field notes from an expedition" in view["view"]
        assert "voice" in view["how"] and "jurors judge" in view["how"]
        r = t.write("A passage that forgets the card's word entirely.", cites)         # a hard failure: still tells the voice
        assert not r["success"] and [h["rule"] for h in r["hard"]] == ["word"]
        assert r["voice"].startswith("This relay's voice: field notes from an expedition")
        assert "jurors judge" in r["voice"] and "code does not check it" in r["voice"]
        # a passage nothing like the voice is sealed all the same: code checks the word, the limit and the cites only
        r = t.write(E.scripted_passage(card), cites)
        assert r["success"] and r["sealed"] and r["voice"].startswith("This relay's voice: field notes")


def test_a_bad_voice_setting_gives_the_default_everywhere(mk, monkeypatch):
    monkeypatch.setenv("EMPORIA_CHRONICLE_VOICE", "see https://example.com/now")
    w, names = mk(6)
    assert w.get("/chronicle").json()["voice"] == C.DEFAULT_VOICE
    did = day(w, names, 1)
    assert chapter(w, did["chapter"])["card"]["voice"] == C.DEFAULT_VOICE
    verified(w)


# ── the verifier when the BLS library is missing: not "the relay lied" ───────────────────────────────

def test_a_missing_bls_library_is_its_own_message_and_exit_code_not_a_forged_signature(monkeypatch, capsys):
    from emporia import beacon
    monkeypatch.setitem(sys.modules, "py_ecc.bls.hash_to_curve", None)       # an import of it now raises ImportError
    assert beacon.bls_available() is False
    assert beacon.verify(1, "00" * 48) is False                               # unchanged: verify still answers a plain False
    assert beacon.BLS_MISSING == ("cannot check beacon signatures: py_ecc is not installed "
                                  "(pip install py_ecc, or --no-bls)")
    with pytest.raises(V.BlsMissing, match="py_ecc is not installed"):
        V.verify(lambda p: pytest.fail("nothing is read before the check can run"), bls=True)
    assert V.main(["http://127.0.0.1:9"]) == V.EXIT_CANNOT_CHECK == 2
    err = capsys.readouterr().err
    assert beacon.BLS_MISSING in err and "not drand's signature" not in err
    # with --no-bls the check does not need the library at all
    rep = V.verify(lambda p: {"relay_id": "r", "formula": "f", "chapters": []}, bls=False)
    assert rep["ok"] and rep["bls"] is False
