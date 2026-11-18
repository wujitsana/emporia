"""Caliber (`emporia.caliber`, `routes/assay.py`, `assay_client.py`): the pure rules, then the Assay on a throwaway relay
with a fake clock and the test beacon. Every test makes its own agents (unique names) and its own rounds."""
from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from emporia import assay_client, caliber, progression, relay_modules, signing, wire
from emporia import relay_server as rs
from emporia.routes import assay as route
from tests.ids import house_env

DAY = 86_400
_runs = itertools.count(1)


# ── the pure rules ───────────────────────────────────────────────────────────────────────────────────────

SIG = hashlib.sha512(b"test-beacon-1").hexdigest()[:96]


def _floyd(item):
    nodes = item["nodes"]
    d = {(a, b): (0 if a == b else 10**9) for a in nodes for b in nodes}
    for a, b, w in item["edges"]:
        d[a, b] = d[b, a] = min(d[a, b], w)
    for k in nodes:
        for i in nodes:
            for j in nodes:
                d[i, j] = min(d[i, j], d[i, k] + d[k, j])
    return d[item["source"], item["target"]]


def test_items_are_deterministic_and_depend_on_every_input():
    a = caliber.item_for(SIG, "2026-11-20", "ann", "ab" * 32, 2)
    assert a == caliber.item_for(SIG, "2026-11-20", "ann", "ab" * 32, 2)
    assert caliber.prompt(a) == caliber.prompt(caliber.item_for(SIG, "2026-11-20", "ann", "ab" * 32, 2))
    others = [caliber.item_for(SIG, "2026-11-20", "bob", "ab" * 32, 2),
              caliber.item_for(SIG, "2026-11-20", "ann", "cd" * 32, 2),
              caliber.item_for(SIG, "2026-11-21", "ann", "ab" * 32, 2),
              caliber.item_for(hashlib.sha512(b"x").hexdigest()[:96], "2026-11-20", "ann", "ab" * 32, 2)]
    assert all(o != a for o in others)


def test_the_checker_is_exact_and_the_solvers_are_right():
    seen = set()
    for i in range(60):
        sd = hashlib.sha256(str(i).encode()).digest()
        for fam in caliber.FAMILIES:
            for tier in caliber.TIERS:
                it = caliber.make_item(sd, fam, tier)
                ans = caliber.solve(it)
                seen.add(fam)
                if fam == "path":
                    assert ans == _floyd(it) and ans > 0
                assert caliber.check(it, ans) and caliber.check(it, f" {ans} ") and caliber.check(it, str(ans))
                assert not caliber.check(it, ans + 1) and not caliber.check(it, f"{ans}.0")
                assert not caliber.check(it, True) and not caliber.check(it, None) and not caliber.check(it, "x" * 40)
    assert seen == set(caliber.FAMILIES)
    sd = hashlib.sha256(b"harder").digest()
    assert len(caliber.make_item(sd, "chain", 3)["ops"]) > len(caliber.make_item(sd, "chain", 1)["ops"])
    assert len(caliber.make_item(sd, "path", 3)["nodes"]) > len(caliber.make_item(sd, "path", 1)["nodes"])


def test_the_salt_commitment_is_checked_on_reveal():
    salt = caliber.new_salt()
    h = caliber.salt_commitment("2026-11-20", salt)
    assert caliber.salt_matches(h, "2026-11-20", salt)
    assert not caliber.salt_matches(h, "2026-11-20", caliber.new_salt())      # another salt
    assert not caliber.salt_matches(h, "2026-11-21", salt)                    # another day
    assert not caliber.salt_matches(h, "2026-11-20", "short")                 # too short to count
    bad = caliber.recompute({"day": "2026-11-20", "agent_id": "ann", "tier": 1}, SIG, caliber.new_salt(), committed=h)
    assert bad["ok"] is False and "does not match" in bad["why"]
    good = caliber.recompute({"day": "2026-11-20", "agent_id": "ann", "tier": 1}, SIG, salt, committed=h)
    assert good["ok"] and good["answer"] == caliber.solve(good["item"])


def _t(day: int, tier: int, passed: bool, conf: int | None = 70) -> dict:
    return {"day": f"d{day}", "at": 1_800_000_000 + day * DAY, "tier": tier, "passed": passed, "confidence": conf}


def test_reason_is_hidden_below_eight_trials_then_shown_with_an_interval():
    now = 1_800_000_000 + 40 * DAY
    seven = [_t(i, 2, i % 2 == 0) for i in range(7)]
    r = caliber.reason(seven, now)
    assert r["shown"] is False and r["n"] == 7 and "estimate" not in r and "1 to go" in r["why"]
    eight = seven + [_t(7, 2, True)]
    r = caliber.reason(eight, now)
    assert r["shown"] and r["n"] == 8 and r["low"] < r["estimate"] < r["high"] and r["last_trial"]
    assert r["interval"] > 0 and "±" in r["line"]
    assert caliber.confidence(seven)["shown"] is False and caliber.confidence(eight)["shown"] is True


def test_reason_moves_with_the_tier_and_the_interval_narrows_with_n():
    now = 1_800_000_000 + 80 * DAY
    easy = caliber.reason([_t(i, 1, True) for i in range(10)], now)
    hard = caliber.reason([_t(i, 3, True) for i in range(10)], now)
    assert hard["estimate"] > easy["estimate"] > 1500
    fails = caliber.reason([_t(i, 3, False) for i in range(10)], now)
    assert fails["estimate"] < 1500 < hard["estimate"]
    mixed = [_t(i, 2, i % 2 == 0) for i in range(60)]
    few, many = caliber.reason(mixed[:10], now), caliber.reason(mixed, now)
    assert many["interval"] < few["interval"]
    # an old reading reads as old: the interval widens with idle time
    stale = caliber.reason(mixed[:10], now + 200 * DAY)
    assert stale["interval"] > few["interval"]
    # the next tier follows the rating
    assert caliber.next_tier([], now) == 2
    assert caliber.next_tier([_t(i, 3, True) for i in range(10)], now) == 3
    assert caliber.next_tier([_t(i, 1, False) for i in range(10)], now) == 1


def test_reliability_per_tier_is_pass_to_the_k():
    rel = caliber.reliability([_t(0, 1, True), _t(1, 1, True), _t(2, 1, True), _t(3, 1, False), _t(4, 3, False)])
    by = {r["tier"]: r for r in rel}
    assert by[1]["n"] == 4 and by[1]["passed"] == 3 and by[1]["k"] == 3 and by[1]["pass_k"] == 0.25   # C(3,3)/C(4,3)
    assert by[3]["pass_k"] == 0.0 and 2 not in by


def test_assay_calls_are_source_assay_and_never_reach_insight():
    trials = [_t(i, 2, i % 3 != 0, conf=80) for i in range(14)] + [_t(14, 2, False, conf=None)]
    calls = caliber.calls_of(trials)
    assert len(calls) == 15 and {c.source for c in calls} == {"assay"} and calls[-1].call is None
    assert calls[-1].edge < 0                         # served and not sealed: the worst call it could have been
    assert all(c.to_dict()["source"] == "assay" for c in calls)
    assert progression.insight_board({"ann": calls}, min_calls=1) == []
    ins = progression.insight("ann", calls)
    assert ins["games"] == 0 and ins["calibration"]["bins"] == []
    game = progression.Call("s1", "emporia:chess:v1", 1.0, 0.6, 0.5, 1.0, 0.2, 1.0, True)
    assert progression.insight_board({"ann": [game] + calls}, min_calls=1)[0]["games"] == 1
    assert "source" not in game.to_dict()
    cal = caliber.confidence(trials)
    assert cal["source"] == "assay" and cal["calibration"]["bins"] and "never ranked alone" in cal["note"]


def test_follow_is_a_pass_rate_with_n_from_fixtures():
    rows = caliber.follow([{"agent": "ann", "ok": True}, {"agent": "ann", "ok": True}, {"agent": "ann", "ok": False},
                           {"agent": "bob", "ok": False}])
    assert rows["ann"]["n"] == 3 and rows["ann"]["passed"] == 2 and rows["ann"]["rate"] == pytest.approx(0.667, abs=1e-3)
    assert rows["bob"] == {**rows["bob"], "n": 1, "passed": 0, "rate": 0.0}
    sh = caliber.sheet("ann", [], rows["ann"], 1_800_000_000)
    assert sh["follow"]["n"] == 3 and sh["label"] == caliber.LABEL and sh["reason"]["shown"] is False


def test_the_board_ranks_by_the_low_end_and_groups_an_operators_agents():
    now = 1_800_000_000 + 60 * DAY
    lucky = [_t(i, 2, True) for i in range(9)]
    solid = [_t(i, 2, i % 4 != 0) for i in range(40)]
    weak = [_t(i, 2, i % 2 == 0) for i in range(9)]
    young = [_t(i, 2, True) for i in range(7)]
    by = {"a1": lucky, "a2": weak, "b1": solid, "c1": young, "house": lucky}
    units = {"a1": "uA", "a2": "uA", "b1": "uB", "c1": "uC", "house": "uH"}
    rows = caliber.board(by, units, {"house"}, now)
    assert [r["unit"] for r in rows if r["unit"] == "uA"] == ["uA"]
    a = next(r for r in rows if r["unit"] == "uA")
    assert {x["agent_id"] for x in a["agents"]} == {"a1", "a2"}                # together, each listed
    assert all(r["unit"] not in ("uC", "uH") for r in rows)                    # under n, and exempt
    lows = [x["low"] for x in a["agents"]]
    assert a["score"] == round(sum(lows) / len(lows))                         # the mean, not the luckiest
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
    assert rows == sorted(rows, key=lambda r: (-r["score"], r["unit"]))


# ── the Assay on a relay ─────────────────────────────────────────────────────────────────────────────────

class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def iso(self) -> str:
        return datetime.fromtimestamp(self.t, UTC).isoformat()


class W:
    def __init__(self, client, clock):
        self.client, self.clock = client, clock
        self.tag = uuid.uuid4().hex[:6]

    def register(self, name: str, operator: str) -> str:
        from emporia.identity import get_public_key_hex
        name = f"as{self.tag}_{name}"
        r = self.client.post("/agents/register", json={"agent_id": name, "public_key_hex": get_public_key_hex(name)})
        assert r.status_code == 200, r.text
        # never later than the real now: the clock may be a year ahead, and a registered_at in the future would sort
        # these agents ahead of every agent a later test registers in the shared DB (GET /agents is newest first)
        with rs.DB_LOCK, rs.get_db() as conn:
            conn.execute("UPDATE authorized_agents SET operator_label = ?, registered_at = ? WHERE agent_id = ?",
                         (f"op{self.tag}_{operator}", datetime.fromtimestamp(min(self.clock.t, time.time()) - 90 * DAY, UTC).isoformat(),
                          name))
            conn.commit()
        rs.forget_who()
        return name

    def hdr(self, agent: str) -> dict:
        import time
        now = time.time()
        stable = rs.AGENT_REGISTRY.get_stable_id(agent) or ""
        tok = rs._jwt_encode({"agent_id": agent, "stable_id": stable, "relay_id": rs.relay_id(), "iat": int(now),
                              "issued": now, "exp": int(now + 3600)})
        return {"Authorization": f"Bearer {tok}"}

    def stable(self, agent: str) -> str:
        return rs.AGENT_REGISTRY.get_stable_id(agent) or ""

    def tool(self, agent: str) -> assay_client.Assay:
        def get(path):
            r = self.client.get(path, headers=self.hdr(agent))
            return r.status_code, r.json()

        def post(path, body):
            r = self.client.post(path, headers=self.hdr(agent), json=body)
            return r.status_code, r.json()
        return assay_client.Assay(agent, get=get, post=post, sign=lambda kind, *a: signing.sign(agent, kind, *a),
                                  stable=lambda: self.stable(agent))

    def raw(self, path: str, agent: str, body: dict):
        return self.client.post(path, headers=self.hdr(agent), json=body)

    def item_of(self, agent: str, day: str | None = None) -> dict:
        """The item the relay served `agent` (rebuilt from the relay's own record, as the answer key)."""
        day = day or route.day_of(self.clock.t)
        with rs.DB_LOCK, rs.get_db() as conn:
            rnd = dict(conn.execute("SELECT * FROM assay_rounds WHERE day = ?", (day,)).fetchone())
            tier = conn.execute("SELECT tier FROM assay_serves WHERE day = ? AND agent_id = ?", (day, agent)).fetchone()[0]
        from conftest import fake_beacon_sig
        return caliber.item_for(fake_beacon_sig(rnd["beacon_round"]), day, agent, rnd["salt"], tier)

    def trial(self, agent: str, *, right: bool = True, confidence: int = 70) -> dict:
        t = self.tool(agent)
        assert t.enter()["success"]
        v = t.view()
        assert v["state"] == "served", v
        it = self.item_of(agent)
        ans = caliber.solve(it) + (0 if right else 1)
        return t.seal(ans, confidence)

    def next_day(self, n: int = 1) -> None:
        self.clock.t = (int(self.clock.t // DAY) + n) * DAY + 3600


@pytest.fixture
def w(monkeypatch):
    from emporia.routes import beacon as beacon_route
    from conftest import fake_beacon_sig
    base = int(__import__("time").time() // DAY) * DAY + 3600 + next(_runs) * 400 * DAY    # a day no other test used
    clock = Clock(base)
    for k, v in {"EMPORIA_ASSAY": "1", "EMPORIA_TRANSPARENCY_LOG": "on", "EMPORIA_GAMIFY": "sheets,quests"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(route, "now", clock)
    monkeypatch.setattr(assay_client, "clock", lambda: datetime.fromtimestamp(clock.t, UTC))
    monkeypatch.setattr(beacon_route, "signature", fake_beacon_sig)
    monkeypatch.setattr(rs.AGENT_REGISTRY, "check_rate_limit", lambda agent_id, *a: True)
    with rs.DB_LOCK, rs.get_db() as conn:
        for t in ("rounds", "reveals", "entries", "serves", "seals"):
            conn.execute(f"DELETE FROM assay_{t}")
        conn.commit()
    world = W(TestClient(rs.app, client=("127.0.0.1", 51900)), clock)
    yield world
    rs.forget_who()


def test_a_day_commits_a_salt_before_serving_and_reveals_it_after(w):
    ann = w.register("ann", "o1")
    card = w.client.get("/assay").json()
    today = card["today"]
    rnd = card["rounds"][0]
    assert rnd["day"] == today and rnd["revealed"] is False and "salt" not in rnd
    assert rnd["salt_hash"] and card["relay_id"] and card["label"] == caliber.LABEL
    out = w.trial(ann)
    assert out["success"] and out["passed"] is True
    assert "salt" not in w.client.get(f"/assay/rounds/{today}").json()
    w.clock.t = route.reveal_time(today) - 5
    assert w.client.get(f"/assay/rounds/{today}").json()["revealed"] is False        # not before the window closes
    w.clock.t = route.reveal_time(today) + 5
    done = w.client.get(f"/assay/rounds/{today}").json()
    assert done["revealed"] and caliber.salt_matches(done["salt_hash"], today, done["salt"])
    # anyone recomputes the item and the result from the public record
    from conftest import fake_beacon_sig
    tr = next(x for x in done["trials"] if x["agent_id"] == ann)
    again = caliber.recompute({"day": today, "agent_id": ann, "tier": tr["tier"], "answer": tr["answer"]},
                              fake_beacon_sig(done["beacon_round"]), done["salt"], committed=done["salt_hash"])
    assert again["ok"] and again["passed"] is True and bool(tr["passed"]) is True and again["item"]["family"] == tr["family"]
    wrong = caliber.recompute({"day": today, "agent_id": ann, "tier": tr["tier"]}, fake_beacon_sig(done["beacon_round"]),
                              caliber.new_salt(), committed=done["salt_hash"])
    assert wrong["ok"] is False                                                        # a salt hash mismatch is refused
    with rs.DB_LOCK, rs.get_db() as conn:                                               # commit, serve, seal and reveal are logged
        recs = [json.loads(r[0]) for r in conn.execute("SELECT record FROM translog_leaves").fetchall()]
    mine = [r for r in recs if "assay" in json.dumps(r) and today in json.dumps(r)]
    text = [json.dumps(r) for r in mine]
    assert any("assay_round" in t for t in text) and any("assay_reveal" in t for t in text)
    assert any("assay_seal" in t for t in text) and any("assay_serve" in t for t in text)
    commit = next(t for t in text if "assay_round" in t)
    assert done["salt"] not in commit and done["salt_hash"] in commit                  # the commit leaves the salt out


def test_entering_comes_before_the_family_is_shown(w):
    ann = w.register("ann", "o1")
    seen = w.raw("/assay/item", ann, {}).json()
    assert seen["state"] == "not_entered" and "prompt" not in seen and "family" not in seen
    assert w.raw("/assay/seal", ann, {"day": route.day_of(w.clock.t), "answer": "1", "confidence": 50,
                                      "at": w.clock.iso(), "signature": "x"}).status_code in (403, 409)
    entered = w.tool(ann).enter()
    assert entered["success"] and entered["entered"] and "prompt" not in entered and "family" not in entered
    assert "family" not in json.dumps(w.client.get("/assay").json()["rounds"])
    served = w.tool(ann).view()
    assert served["state"] == "served" and served["family"] in caliber.FAMILIES and served["prompt"]
    assert served["seconds_left"] <= caliber.SEAL_WINDOW_S
    assert w.tool(ann).enter()["already"] is True                                      # entering twice is one entry


def test_the_beacon_round_must_be_out_before_an_item_is_served(w, monkeypatch):
    from emporia.routes import beacon as beacon_route
    ann = w.register("ann", "o1")
    w.tool(ann).enter()
    monkeypatch.setattr(beacon_route, "signature", lambda r: None)
    waiting = w.tool(ann).view()
    assert waiting["state"] == "waiting" and waiting["retry_in_s"] >= 1 and "prompt" not in waiting


def test_an_unreachable_beacon_after_its_round_is_a_slow_retry_not_a_second_one(w, monkeypatch):
    from emporia.routes import beacon as beacon_route
    ann = w.register("ann", "o1")
    w.tool(ann).enter()
    monkeypatch.setattr(beacon_route, "signature", lambda r: None)
    monkeypatch.setattr(route.rand, "round_time", lambda r: w.clock.t - 100)
    waiting = w.tool(ann).view()
    assert waiting["state"] == "waiting" and waiting["retry_in_s"] == 30 and "not reachable" in waiting["note"]


def test_an_item_served_and_not_sealed_counts_as_a_fail(w):
    ann = w.register("ann", "o1")
    w.tool(ann).enter()
    assert w.tool(ann).view()["state"] == "served"
    with rs.DB_LOCK, rs.get_db() as conn:
        assert route.trials_by_agent(conn, w.clock.t).get(ann) is None                  # still open: not yet a trial
    w.clock.t += caliber.SEAL_WINDOW_S + 1
    with rs.DB_LOCK, rs.get_db() as conn:
        t = route.trials_by_agent(conn, w.clock.t)[ann]
    assert len(t) == 1 and t[0]["passed"] is False and t[0]["sealed"] is False and t[0]["confidence"] is None
    assert w.tool(ann).view()["state"] == "expired"
    late = w.tool(ann).seal(3, 50)
    assert late["success"] is False and "closed" in json.dumps(late["detail"])
    w.next_day()                                                                        # the fail stays in the record
    assert caliber.calls_of(t)[0].call is None and caliber.calls_of(t)[0].outcome == 0.0


def test_one_trial_a_day(w):
    ann = w.register("ann", "o1")
    assert w.trial(ann)["success"]
    assert w.tool(ann).seal(5, 60)["success"] is False                                 # the tool sees it is done
    day, at = route.day_of(w.clock.t), w.clock.iso()
    sig = signing.sign(ann, "assay-seal", rs.relay_id(), day, "5", 60, w.stable(ann), at)
    r = w.raw("/assay/seal", ann, {"day": day, "answer": "5", "confidence": 60, "at": at, "signature": sig})
    assert r.status_code == 409 and "already sealed" in r.text                         # and so does the relay
    assert w.tool(ann).view()["state"] == "sealed"
    assert w.tool(ann).enter()["already"] is True
    w.next_day()
    assert w.trial(ann, right=False)["passed"] is False                                 # tomorrow is a new trial
    with rs.DB_LOCK, rs.get_db() as conn:
        assert len(route.trials_by_agent(conn, w.clock.t)[ann]) == 2


def test_the_seal_is_checked_and_signed(w):
    ann, bob = w.register("ann", "o1"), w.register("bob", "o2")
    t = w.tool(ann)
    t.enter()
    t.view()
    assert t.seal("seven", 50)["success"] is False and t.seal(5, 0)["success"] is False and t.seal(5, 100)["success"] is False
    day = route.day_of(w.clock.t)
    forged = {"day": day, "answer": "5", "confidence": 50, "at": w.clock.iso(), "signature": "AAAA"}
    assert w.raw("/assay/seal", ann, forged).status_code == 403
    # an agent that was never served cannot seal
    sig = signing.sign(bob, "assay-seal", rs.relay_id(), day, "5", 50, w.stable(bob), w.clock.iso())
    r = w.raw("/assay/seal", bob, {"day": day, "answer": "5", "confidence": 50, "at": w.clock.iso(), "signature": sig})
    assert r.status_code == 409
    old = w.clock.iso()
    sig = signing.sign(ann, "assay-enter", rs.relay_id(), day, w.stable(ann), old)
    w.clock.t += 3 * 3600
    assert w.raw("/assay/enter", ann, {"day": day, "at": old, "signature": sig}).status_code == 400   # a stale signature


def test_a_day_that_is_not_today_cannot_be_entered(w):
    ann = w.register("ann", "o1")
    other = route.day_of(w.clock.t + 2 * DAY)
    sig = signing.sign(ann, "assay-enter", rs.relay_id(), other, w.stable(ann), w.clock.iso())
    assert w.raw("/assay/enter", ann, {"day": other, "at": w.clock.iso(), "signature": sig}).status_code == 409


def _seed(agent: str, trials: list[dict]) -> None:
    """Trial records straight into the tables (to give an agent a history without eight days of play)."""
    with rs.DB_LOCK, rs.get_db() as conn:
        for _i, t in enumerate(trials):
            day = t["day"]
            conn.execute("INSERT OR REPLACE INTO assay_serves VALUES (?,?,?,?,?,?)",
                         (day, agent, "chain", t["tier"], t["at"], t["at"] + 900))
            if t["confidence"] is not None:
                conn.execute("INSERT OR REPLACE INTO assay_seals VALUES (?,?,?,?,?,?,?,?,?)",
                             (day, agent, "s", "1", t["confidence"], int(t["passed"]), "x", t["at"] + 60, "sig"))
        conn.commit()


def _history(w: W, n: int, tier: int = 2, passed=lambda i: True) -> list[dict]:
    start = int(w.clock.t // DAY) - 100
    return [{"day": f"2020-01-{i + 1:02d}", "at": (start + i) * DAY + 100, "tier": tier, "passed": passed(i),
             "confidence": 70} for i in range(n)]


def test_the_sheet_shows_caliber_from_eight_trials_and_the_label(w):
    ann = w.register("ann", "o1")
    _seed(ann, _history(w, 7))
    sheet = w.client.get(f"/agents/{ann}/sheet").json()
    cal = sheet["caliber"]
    assert cal["formula"] == "caliber:v1" and cal["label"] == caliber.LABEL
    assert cal["reason"]["shown"] is False and cal["reason"]["n"] == 7
    _seed(ann, _history(w, 9, passed=lambda i: i != 3))
    rs._WHO_CACHE.clear()
    got = w.client.get(f"/agents/{ann}/caliber").json()
    assert got["reason"]["shown"] is True and got["reason"]["n"] == 9 and got["reason"]["interval"] > 0
    assert got["confidence"]["shown"] and got["confidence"]["source"] == "assay"
    text = progression.render({**sheet, "caliber": got})
    assert "Caliber" in text and "Reason" in text and "whole setup" in text and "never ranked alone" in text


def test_the_board_leaves_out_house_and_operator_agents_and_groups_by_operator(w, monkeypatch):
    a1, a2, b1, h = (w.register("a1", "oa"), w.register("a2", "oa"), w.register("b1", "ob"), w.register("hh", "oh"))
    op = w.register("opr", "oo")
    house_env(monkeypatch, h)
    from tests.ids import stable_of
    monkeypatch.setenv("EMPORIA_OPERATOR_STABLE_ID", stable_of(op))
    for a in (a1, a2, b1, h, op):
        _seed(a, _history(w, 12))
    board = w.client.get("/caliber").json()
    ranked = [x["agent_id"] for r in board["board"] for x in r["agents"]]
    assert set(ranked) == {a1, a2, b1}                                  # house and the relay operator's agent: off
    both = next(r for r in board["board"] if {x["agent_id"] for x in r["agents"]} == {a1, a2})
    assert len(both["agents"]) == 2 and both["unit"].startswith("u") and "oa" not in both["unit"]
    mine = w.client.get(f"/agents/{h}/caliber").json()
    assert mine["on_board"] is False and "kept off boards" in mine["off_board"]
    assert w.client.get(f"/agents/{a1}/caliber").json()["on_board"] is True


def test_follow_runs_the_chronicles_card_checks_over_each_agents_passages(w, monkeypatch):
    from emporia.routes import chronicle as ch
    ann, bob = w.register("ann", "o1"), w.register("bob", "o2")
    slots = [{"windows": {"count": [0, 10]}, "n": i} for i in range(3)] + [{"windows": {"count": [0, 10**12]}}]
    verdicts = {0: {ann: None, bob: ["word"]}, 1: {ann: None}, 2: {ann: ["shape"], bob: None}}

    def passages(ledger, slot):
        return {f"{k}{slot['n']}": {"author": k, "set_aside": v} for k, v in verdicts[slot["n"]].items()}
    monkeypatch.setattr(relay_modules, "off_because", lambda m, p: "")
    monkeypatch.setattr(ch, "sequence", lambda: ({}, slots, 100.0))
    monkeypatch.setattr(ch, "_passages", passages)
    rows = route.follow_rows(frozenset())
    assert rows[ann]["n"] == 3 and rows[ann]["passed"] == 2 and rows[bob]["n"] == 2 and rows[bob]["passed"] == 1
    assert route.follow_rows(frozenset({bob})).get(bob) is None                # the house is left out
    assert w.client.get(f"/agents/{ann}/caliber").json()["follow"]["n"] == 3


def test_caliber_never_changes_allowance_or_xp(w):
    from emporia.routes import standing as st
    ann = w.register("ann", "o1")

    def reading():
        st.forget_levels()
        a = st.allowance_of(ann)
        xp = sum(q["xp"] for q in st.quests_body(ann)["quests"] if q["done"])
        return (a or {}).get("tier"), (a or {}).get("limits"), xp, st.agent_level(ann)
    before = reading()
    _seed(ann, _history(w, 12))
    for _ in range(3):
        assert w.trial(ann)["success"]
        w.next_day()
    assert w.client.get(f"/agents/{ann}/caliber").json()["reason"]["shown"]
    assert reading() == before


def test_the_module_is_off_by_default_and_a_wrong_switch_is_refused(monkeypatch):
    monkeypatch.setenv("EMPORIA_ASSAY", "0")
    mods = relay_modules.advertise()
    assert "assay" not in mods and set(relay_modules.ASSAY_TOOLS) <= relay_modules.tools_to_hide([mods])
    client = TestClient(rs.app, client=("127.0.0.1", 51901))
    assert client.get("/assay").status_code == 404 and client.get("/caliber").status_code == 404
    monkeypatch.setenv("EMPORIA_ASSAY", "maybe")
    assert any("EMPORIA_ASSAY" in p for p in relay_modules.problems())
    monkeypatch.setenv("EMPORIA_ASSAY", "1")
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "off")
    assert relay_modules.running().get("assay") is None                                # it needs the log
    monkeypatch.setenv("EMPORIA_TRANSPARENCY_LOG", "on")
    assert relay_modules.advertise()["assay"]["parts"] == ["trials"]


def test_schema_58_adds_the_assay_tables_and_upgrade_carries_an_old_database(tmp_path):
    from emporia import schema_upgrade as U
    from emporia.schema import SCHEMA_VERSION
    from tests.test_schema_upgrade import make_old
    assert SCHEMA_VERSION >= 58
    old = make_old(tmp_path / "emporia.sqlite3")
    assert any("assay_rounds" in line for line in U.dry_run(old).lines())
    U.upgrade(old)
    conn = sqlite3.connect(old)
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"assay_rounds", "assay_reveals", "assay_entries", "assay_serves", "assay_seals"} <= names
        assert conn.execute("SELECT COUNT(*) FROM assay_seals").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_signed_kinds_are_in_the_signing_table_and_wire():
    for kind in ("assay-enter", "assay-seal"):
        assert kind in signing.KINDS and not signing.KINDS[kind].person
    p = wire.signed_assay_seal_payload("r", "2026-11-20", "42", 70, "stable", "2026-11-20T00:00:00+00:00")
    assert p["type"] == "emporia:assay-seal:v1" and p["confidence"] == 70
    assert wire.signed_assay_enter_payload("r", "2026-11-20", "s", "t")["type"] == "emporia:assay-enter:v1"


# ── Finish: a floor, from records that already exist ─────────────────────────────────────────────────────

def _ob(agent, ok, kind="game", by="agent", at="2026-11-10T10:00:00+00:00"):
    return {"agent": agent, "kind": kind, "ok": ok, "by": by, "at": at}


def test_finish_is_a_pass_rate_with_n_the_split_and_the_last_date():
    rows = [_ob("ann", True)] * 5 + [_ob("ann", True, "reveal", "bridge")] * 2 + [_ob("ann", False, "clock")] \
        + [_ob("ann", False, "assay", at="2026-11-12T00:00:00+00:00")]
    got = caliber.finish(rows)["ann"]
    assert got["shown"] and got["n"] == 9 and got["passed"] == 7 and got["rate"] == pytest.approx(0.778, abs=1e-3)
    assert got["by_agent"] == 5 and got["by_bridge"] == 2
    assert got["missed"] == {"clock": 1, "assay": 1} and got["last"] == "2026-11-12"
    assert "bridge" in got["bridge_note"] and "setup finishing" in got["bridge_note"]
    assert "never ranked" in got["note"]


def test_finish_is_hidden_below_the_threshold_and_empty_is_n_zero():
    rows = [_ob("bob", True)] * (caliber.MIN_FINISH - 1)
    got = caliber.finish(rows)["bob"]
    assert got["shown"] is False and got["n"] == caliber.MIN_FINISH - 1 and "rate" not in got and got["passed"] is None
    empty = caliber.finish_row({}, "nobody")
    assert empty["n"] == 0 and empty["shown"] is False
    assert caliber.finish([*rows, _ob("bob", False)])["bob"]["rate"] == pytest.approx(0.875)


def test_the_bridge_marker_is_read_through_one_function():
    assert caliber.done_by_bridge({"by": "bridge", "x": 1}) is True
    assert not caliber.done_by_bridge({"by": "agent"}) and not caliber.done_by_bridge({}) and not caliber.done_by_bridge(None)
    assert (caliber.BRIDGE_KEY, caliber.BRIDGE_VALUE) == ("by", "bridge")


def _obl(agent="ann", **kw):
    base = dict(module="emporia:tesseract:v1", status="completed", participants=["ann", "zed"], outcome={},
                quitter=None, actions=[], at="2026-11-10T00:00:00+00:00")
    base.update(kw)
    return caliber.session_obligation(agent, **base)


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
    for src in (inspect.getsource(allowance), inspect.getsource(caliber.board)):
        assert '"finish"' not in src and "'finish'" not in src and "caliber.finish" not in src
    assert "finish" not in json.dumps(caliber.board({ann: _history(w, 12)}, {}, [], 1_800_000_000))


def test_an_old_sheet_without_finish_still_renders():
    text = progression.render({"agent_id": "ann", "craft": {}, "caliber": None})
    assert "Finish" not in text
    shown = caliber.finish([_ob("ann", True)] * 9 + [_ob("ann", True, "reveal", "bridge")])["ann"]
    out = "\n".join(progression._render_finish(shown, None))
    assert "10 of 10 obligations completed" in out and "1 by its bridge" in out and "setup finishing" in out
    assert "so far" in "\n".join(progression._render_finish(caliber.finish([_ob("ann", True)])["ann"], None))
    assert progression._render_finish(None, None) == []


def test_the_sheet_carries_finish_additively_and_hidden_for_a_new_agent(w):
    ann = w.register("ann", "o1")
    sheet = w.client.get(f"/agents/{ann}/sheet").json()
    assert sheet["finish"]["n"] == 0 and sheet["finish"]["shown"] is False and "never ranked" in sheet["finish"]["note"]
    assert "Finish" not in progression.render(sheet)                      # nothing to say yet


def test_finish_reads_the_bridge_mark_from_the_signed_rationale_in_the_audit_log(monkeypatch):
    """The bridge marks an open by its signed rationale (`[bridge] ...`), which the relay keeps in the session's
    public audit log, not in `actions`: Finish joins it in and counts the step as the bridge's."""
    from emporia import caliber
    from emporia.routes import standing
    from emporia.tesseract_client import BRIDGE_MARK
    log = [{"sender": "a", "action": "seal", "rationale": "sealing"},
           {"sender": "a", "action": "open", "rationale": BRIDGE_MARK + "opening my sealed push of round 1"},
           {"sender": "b", "action": "open", "rationale": "mine"}]
    monkeypatch.setattr("emporia.session_audit.get_public_log", lambda sid: log)
    acts = {"s1": [("seal", {"commitment": "c"}), ("open", {"move": [1], "nonce": "n"})]}
    standing._join_bridge_marks("a", acts)
    assert caliber.done_by_bridge(acts["s1"][1][1])
    assert not caliber.done_by_bridge({"move": [1], "rationale": "opened by hand"})
    assert caliber.done_by_bridge({"by": "bridge"})
