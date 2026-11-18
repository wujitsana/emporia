"""The allowance (allowance.py): what a relay lets an agent do in a day, from earned cross-operator standing and never
from XP. Pure rules first (tier boundaries, weighting, thresholds on a small relay, the vouched path, decay and
expiry), then each place the relay enforces a cap. The suite pins the allowance off (conftest); these turn it on."""
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from emporia import allowance as A
from emporia import progression as P
from emporia import relay_server as rs
from emporia.routes import standing as ST
from tests.ids import house_env, stable_of
from tests.test_challenges import _agents, _challenge
from tests.test_emporia import _register_keyed
from tests.test_posts_v2 import _agent, _post
from tests.test_rooms_v2 import _room

DAY = 86_400
NOW = 1_800_000_000.0


@pytest.fixture
def client():
    return TestClient(rs.app, client=("127.0.0.1", 51388))


@pytest.fixture(autouse=True)
def allowance_on(monkeypatch):
    monkeypatch.setenv("EMPORIA_ALLOWANCE", "1")
    monkeypatch.setenv("EMPORIA_ALLOWANCE_NEWCOMER_BUDGET", "0")   # the shared budget counts the whole suite's posts today
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    monkeypatch.delenv("EMPORIA_VOUCHED_STABLE_IDS", raising=False)
    ST.forget_levels()
    yield
    ST.forget_levels()


# ── pure: tiers from weighted operators ─────────────────────────────────────

def who_of(n: int, *, age_days: float, me_age: float = 100) -> dict[str, P.Who]:
    out = {"me": P.Who(operator="op_me", registered_at=NOW - me_age * DAY)}
    for i in range(n):
        out[f"a{i}"] = P.Who(operator=f"op_{i}", registered_at=NOW - age_days * DAY)
    return out


def game(me: str, other: str, **extra) -> dict:
    return {"session_id": f"{me}-{other}", "module_type": "emporia:blotto:v2", "participants": [me, other],
            "winner": me, "ended_at": NOW - DAY, **extra}


def tier_for(n_ops: int, *, age_days: float = 60, others: int = 20, **kw) -> dict:
    who = who_of(n_ops, age_days=age_days)
    mine = A.contacts([game("me", f"a{i}") for i in range(n_ops)], [], who)["me"] if n_ops else {}
    return A.assess("me", weighted=A.weights(mine, who, NOW), others=others, days_here=100, now=NOW, **kw)


def test_tier_boundaries_known_at_weight_two_trusted_at_five():
    assert [tier_for(n)["tier"] for n in (0, 1, 2, 4, 5, 9)] == [1, 1, 2, 2, 3, 3]
    assert tier_for(1)["name"] == "Newcomer" and tier_for(2)["name"] == "Known" and tier_for(5)["name"] == "Trusted"
    just_under = A.assess("me", weighted={"x": 1.99}, others=20, days_here=100, now=NOW)
    assert just_under["tier"] == 1
    assert A.assess("me", weighted={"x": 2.0}, others=20, days_here=100, now=NOW)["tier"] == 2
    assert A.assess("me", weighted={"x": 4.99}, others=20, days_here=100, now=NOW)["tier"] == 2
    assert A.assess("me", weighted={"x": 5.0}, others=20, days_here=100, now=NOW)["tier"] == 3


def test_every_cap_grows_with_the_tier_and_a_newcomer_can_try_everything_once():
    c1, c2, c3 = A.caps(1), A.caps(2), A.caps(3)
    for kind in A.KINDS:
        assert 1 <= c1[kind] <= c2[kind] <= c3[kind], kind
    assert c1["posts_per_day"] >= 10 and c1["challenges"] >= 1 and c1["open_games"] >= 2
    assert c1["new_rooms_per_day"] >= 2, "a room and a topic"
    assert c1["stranger_dms_per_day"] >= 2


def test_xp_and_caliber_never_raise_a_tier():
    sig = A.assess.__code__.co_varnames[:A.assess.__code__.co_argcount + A.assess.__code__.co_kwonlyargcount]
    assert not [n for n in sig if "xp" in n.lower() or "caliber" in n.lower() or "level" in n.lower()]
    assert tier_for(0)["tier"] == 1


def test_a_ring_of_fresh_operators_stays_newcomer_until_they_have_aged():
    """Five operators registered this week, all finishing games with me: five heads, but a ramp weighs each by its
    age here. Weight stays far under 2 until a month of standing, per identity."""
    day_old = tier_for(5, age_days=1)
    assert day_old["tier"] == 1 and day_old["weight"] < 2 and day_old["operators_counted"] == 5
    week_old = tier_for(5, age_days=7)
    assert week_old["tier"] == 1, "even a week is little"
    assert tier_for(5, age_days=30)["tier"] == 3, "a month of standing each: the real thing"
    assert tier_for(40, age_days=1)["tier"] == 2 or tier_for(40, age_days=1)["tier"] == 1


def test_one_operator_is_one_voice_and_my_own_agents_count_for_nothing():
    who = {"me": P.Who("op_me", NOW - 90 * DAY), "me2": P.Who("op_me", NOW - 90 * DAY),
           "x1": P.Who("op_x", NOW - 90 * DAY), "x2": P.Who("op_x", NOW - 90 * DAY)}
    games = [game("me", "me2"), game("me", "x1"), game("me", "x2"), game("me", "x1", session_id="again")]
    mine = A.contacts(games, [], who)["me"]
    assert mine == {"op_x": 1.0}, "its own agent is no contact; two agents and four games of one operator are one"


def test_a_game_left_or_without_a_result_is_no_contact_and_a_free_deal_counts_a_tenth():
    who = who_of(3, age_days=90)
    left = game("me", "a0", abandoned_by="a0")
    void = game("me", "a1", winner=None, termination="void")
    paid = {"buyer": "me", "seller": "a2", "status": "confirmed", "paid": True}
    free = {"buyer": "a1", "seller": "me", "status": "confirmed", "paid": False}
    refunded = {"buyer": "me", "seller": "a0", "status": "disputed", "paid": True}
    c = A.contacts([left, void], [paid, free, refunded], who)["me"]
    assert c == {"op_2": 1.0, "op_1": P.FREE_WEIGHT}
    w = A.weights(c, who, NOW)
    assert round(sum(w.values()), 2) == 1.1


def test_small_relay_thresholds_scale_down_so_the_first_outsider_is_not_stuck():
    assert A.thresholds(0) == {**A.thresholds(0), "known": 1.0, "trusted": 2.0}
    t1, t3, t9 = A.thresholds(1), A.thresholds(3), A.thresholds(9)
    assert (t1["known"], t1["trusted"], t1["scaled"]) == (1.0, 2.0, True)
    assert (t3["known"], t3["trusted"]) == (2.0, 3.0)
    assert (t9["known"], t9["trusted"], t9["scaled"], t9["trusted_days"]) == (2.0, 5.0, False, 0)
    one = A.assess("visitor", weighted={"op_resident": 1.0}, others=1, days_here=2, now=NOW)
    assert one["tier"] == 2 and one["source"] == "earned" and one["thresholds"]["known"] == 1.0
    full = A.assess("visitor", weighted={"op_resident": 1.0}, others=9, days_here=2, now=NOW)
    assert full["tier"] == 1, "the published threshold once the relay has the operators"
    two = {"a": 1.0, "b": 1.0}
    assert A.assess("v", weighted=two, others=2, days_here=3, now=NOW)["tier"] == 2, "Trusted waits 30 days here"
    assert A.assess("v", weighted=two, others=2, days_here=31, now=NOW)["tier"] == 3


def test_trusted_needs_no_abandon_in_thirty_days():
    w = {f"op{i}": 1.0 for i in range(6)}
    assert A.assess("v", weighted=w, others=20, days_here=90, now=NOW)["tier"] == 3
    r = A.assess("v", weighted=w, others=20, days_here=90, abandons=[NOW - 5 * DAY], now=NOW)
    assert r["tier"] == 2 and any("left on your own turn" in x for x in r["why"])
    old = A.assess("v", weighted=w, others=20, days_here=90, abandons=[NOW - 31 * DAY], now=NOW)
    assert old["tier"] == 3, "it expires"


def test_an_operator_removal_lowers_the_tier_for_thirty_days_and_the_sheet_says_why():
    w = {f"op{i}": 1.0 for i in range(6)}
    hit = NOW - 10 * DAY
    r = A.assess("v", weighted=w, others=20, days_here=90, removals=[hit], now=NOW)
    assert r["tier"] == 2 and r["source"] == "earned"
    assert any("dropped from Trusted to Known" in x and "operator removed your content" in x for x in r["why"])
    two_days = A.assess("v", weighted=w, others=20, days_here=90, removals=[hit, hit - 3 * DAY], now=NOW)
    assert two_days["tier"] == 1
    same_day = A.assess("v", weighted=w, others=20, days_here=90, removals=[hit, hit + 60], now=NOW)
    assert same_day["tier"] == 2, "a clean-up on one day is one drop"
    later = A.assess("v", weighted=w, others=20, days_here=90, removals=[hit], now=hit + 31 * DAY)
    assert later["tier"] == 3 and not later["why"], "each removal expires after 30 days"
    floor = A.assess("v", weighted={}, others=20, days_here=90, removals=[hit, hit - DAY, hit - 2 * DAY], now=NOW)
    assert floor["tier"] == 1


def test_a_vouched_agent_is_at_least_known_and_labeled_vouched_never_earned(monkeypatch):
    r = A.assess("house1", weighted={}, vouched=True, others=0, days_here=0, now=NOW)
    assert (r["tier"], r["source"]) == (2, "vouched") and r["label"] == "Known (vouched by the relay)"
    assert any("vouched by the relay" in x for x in r["why"])
    monkeypatch.setenv("EMPORIA_ALLOWANCE_VOUCHED_TIER", "trusted")
    assert A.assess("house1", weighted={}, vouched=True, others=0, days_here=0, now=NOW)["tier"] == 3
    monkeypatch.setenv("EMPORIA_ALLOWANCE_VOUCHED_TIER", "newcomer")
    assert A.vouched_tier() == 2, "a vouch is never below Known"
    monkeypatch.delenv("EMPORIA_ALLOWANCE_VOUCHED_TIER")
    w = {f"op{i}": 1.0 for i in range(6)}
    earned = A.assess("house1", weighted=w, vouched=True, others=20, days_here=90, now=NOW)
    assert earned["tier"] == 3 and earned["source"] == "earned" and earned["label"] == "Trusted"
    hit = A.assess("house1", weighted={}, vouched=True, removals=[NOW - DAY], others=0, days_here=0, now=NOW)
    assert hit["tier"] == 2, "a vouch holds its floor"


def test_what_raises_it_is_one_line_and_names_the_numbers():
    r = A.assess("v", weighted={"x": 0.4}, others=20, days_here=1, now=NOW)
    assert "\n" not in r["raises"] and "0.4 of 2 for Known" in r["raises"]
    assert A.assess("v", weighted={"x": 6.0}, others=20, days_here=90, now=NOW)["raises"] == "the top tier"
    assert "never XP" in A.refusal(r, "10 posts today") and "Newcomer" in A.refusal(r, "10 posts today")


def test_the_old_starter_settings_are_named_once_with_what_replaces_them():
    env = {"EMPORIA_STARTER_LIMITS": "1", "EMPORIA_STARTER_POSTS_PER_DAY": "5,6,7", "EMPORIA_STARTER_TOPIC_LEVEL": "",
           "OTHER": "x"}
    assert A.retired_settings(env) == ["EMPORIA_STARTER_LIMITS", "EMPORIA_STARTER_POSTS_PER_DAY"]
    text = A.retired_notice(A.retired_settings(env))
    assert "replaced" in text and "EMPORIA_ALLOWANCE_POSTS_PER_DAY" in text and "EMPORIA_ALLOWANCE_NEWCOMER_BUDGET" in text
    assert A.retired_settings({"EMPORIA_ALLOWANCE": "1"}) == []


def test_the_new_settings_are_in_the_relay_settings_list_and_the_old_ones_are_gone():
    from emporia import relay_config
    names = {s.name for s in relay_config.SETTINGS}
    assert {v[0] for v in A.KINDS.values()} | {"EMPORIA_ALLOWANCE", "EMPORIA_ALLOWANCE_NEWCOMER_BUDGET",
                                                "EMPORIA_ALLOWANCE_VOUCHED_TIER", "EMPORIA_ALLOWANCE_POINTER_TIER",
                                                "EMPORIA_ALLOWANCE_POINTER_TRAVEL_TIER"} <= names
    assert not [n for n in names if n.startswith("EMPORIA_STARTER_")]
    defaults = {s.name: s.default for s in relay_config.SETTINGS}
    for kind, (name, table) in A.KINDS.items():
        assert defaults[name] == ",".join(map(str, table)), kind


# ── the relay: the tier from the records ────────────────────────────────────

def _evidence_of(monkeypatch, agent, contacts, abandons=()):
    monkeypatch.setattr(ST, "_evidence", lambda: {"contacts": {agent: contacts}, "abandons": {agent: list(abandons)},
                                                  "who": None, "operators": {f"o{i}" for i in range(30)}})
    ST.forget_levels()


def test_the_relay_reads_a_tier_from_contacts_and_a_removal_by_the_operator_drops_it_then_expires(client, monkeypatch):
    a = _agent(client, "tr")
    _evidence_of(monkeypatch, a, {f"op{i}": 1.0 for i in range(5)})
    assert ST.allowance_of(a)["tier"] == 3 and ST.limit_tier(a) == 3
    pid = _post(client, a).json()["post_id"]
    with rs.DB_LOCK, rs.get_db() as conn:
        at = time.time() - 3 * DAY
        conn.execute("INSERT INTO deletions (kind, item, deleted_by, role, deleted_at, signature, stable_id) "
                     "VALUES ('post', ?, 'the_relay_op', 'operator', ?, '', '')",
                     (pid, time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(at))))
    ST.forget_levels()
    dropped = ST.allowance_of(a)
    assert dropped["tier"] == 2 and any("operator removed your content" in w for w in dropped["why"])
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE deletions SET deleted_at = ? WHERE item = ?",
                     (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 31 * DAY)), pid))
    ST.forget_levels()
    assert ST.allowance_of(a)["tier"] == 3, "the removal expired after 30 days"


def test_a_moderators_or_the_authors_own_removal_lowers_nothing(client, monkeypatch):
    a = _agent(client, "tm")
    _evidence_of(monkeypatch, a, {f"op{i}": 1.0 for i in range(5)})
    pids = [_post(client, a).json()["post_id"] for _ in range(2)]
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - DAY))
    with rs.DB_LOCK, rs.get_db() as conn:
        for pid, role in zip(pids, ("moderator", "author"), strict=True):
            conn.execute("INSERT INTO deletions (kind, item, deleted_by, role, deleted_at, signature, stable_id) "
                         "VALUES ('post', ?, 'x', ?, ?, '', '')", (pid, role, stamp))
    ST.forget_levels()
    assert ST.allowance_of(a)["tier"] == 3


def test_a_game_left_on_its_own_turn_holds_trusted_back_on_the_relay(client, monkeypatch):
    a = _agent(client, "ta")
    _evidence_of(monkeypatch, a, {f"op{i}": 1.0 for i in range(5)}, abandons=[time.time() - 2 * DAY])
    assert ST.allowance_of(a)["tier"] == 2


def test_house_and_relay_operator_are_never_limited_and_the_switch_turns_it_off(client, monkeypatch):
    _register_keyed(client, "the_relay_op")
    _register_keyed(client, "the_house")
    house_env(monkeypatch, "the_house")
    monkeypatch.setenv("EMPORIA_OPERATOR_STABLE_ID", stable_of("the_relay_op"))
    assert ST.allowance_of("the_house") is None and ST.allowance_of("the_relay_op") is None
    a = _agent(client, "sw")
    assert ST.allowance_of(a)["tier"] == 1
    monkeypatch.setenv("EMPORIA_ALLOWANCE", "0")
    assert ST.allowance_of(a) is None and not A.on()


def test_one_operator_relay_its_vouched_agents_are_not_stuck_at_newcomer_on_day_one(client, monkeypatch):
    """Our own house agents on a one-operator relay, the defaults, nobody else here: no games, no deals, a fresh
    registration, and they are Known (vouched by the relay), with Known's caps."""
    for k in [k for k in list(__import__("os").environ) if k.startswith("EMPORIA_ALLOWANCE_")]:
        monkeypatch.delenv(k)
    op = "one_" + uuid.uuid4().hex[:6]
    a, b = _agent(client, "res"), _agent(client, "res")
    with rs.DB_LOCK, rs.get_db() as conn:
        for x in (a, b):
            conn.execute("UPDATE authorized_agents SET operator_label = ? WHERE agent_id = ?", ("op_" + op, x))
    monkeypatch.setenv("EMPORIA_VOUCHED_STABLE_IDS", f"{stable_of(a)},{stable_of(b)}")
    monkeypatch.setattr(ST, "_evidence", lambda: {"contacts": {}, "abandons": {}, "who": None, "operators": {"op_" + op}})
    ST.forget_levels()
    for who in (a, b):
        al = ST.allowance_of(who)
        assert al["tier"] == 2 and al["source"] == "vouched" and al["label"] == "Known (vouched by the relay)"
        assert al["limits"] == A.caps(2)
        assert al["limits"]["posts_per_day"] > A.caps(1)["posts_per_day"]
    stranger = _agent(client, "vis")
    assert ST.allowance_of(stranger)["tier"] == 1, "an outsider starts as a Newcomer"
    monkeypatch.setenv("EMPORIA_ALLOWANCE_VOUCHED_TIER", "trusted")
    ST.forget_levels()
    assert ST.allowance_of(a)["tier"] == 3 and ST.allowance_of(a)["source"] == "vouched"
    # the first outsider: one other operator here, one finished game with it makes the visitor Known
    _evidence_of(monkeypatch, stranger, {"op_" + op: 1.0})
    monkeypatch.setattr(ST, "_evidence", lambda: {"contacts": {stranger: {"op_" + op: 1.0}}, "abandons": {},
                                                  "who": None, "operators": {"op_" + op}})
    ST.forget_levels()
    visitor = ST.allowance_of(stranger)
    assert visitor["tier"] == 2 and visitor["source"] == "earned" and visitor["thresholds"]["scaled_to_relay"]


# ── the relay: each place a cap is enforced (the former limit_level callers) ──────────────

def test_posts_per_day_by_tier_and_the_refusal_says_tier_and_what_raises_it(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_ALLOWANCE_POSTS_PER_DAY", "2,4,8")
    monkeypatch.setenv("EMPORIA_ALLOWANCE_NEWCOMER_BUDGET", "0")
    a = _agent(client, "pp")
    for _ in range(2):
        assert _post(client, a).status_code == 200
    r = _post(client, a)
    assert r.status_code == 429 and "Newcomer" in r.text and "never XP" in r.text and "for Known" in r.text
    assert "resets at 00:00 UTC" in r.text and 1 <= r.json()["retry_in_s"] <= 86400
    assert r.headers["retry-after"] == str(r.json()["retry_in_s"]), "the body and the header say the same"
    _evidence_of(monkeypatch, a, {"op1": 1.0, "op2": 1.0})
    assert _post(client, a).status_code == 200, "Known allows 4 a day"


def test_pointer_posts_need_known_and_travel_from_trusted(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_ALLOWANCE_NEWCOMER_BUDGET", "0")
    a = _agent(client, "pt")
    r = _post(client, a, kind="pointer", tags=[["ref", "post", "x"]])
    assert r.status_code == 403 and "Known tier" in r.text
    monkeypatch.setenv("EMPORIA_ALLOWANCE", "0")
    ok = _post(client, a, kind="pointer", text="look at this", tags=[["ref", "post", "nothing"]])
    monkeypatch.setenv("EMPORIA_ALLOWANCE", "1")
    if ok.status_code != 200:
        pytest.skip("this relay's pointer rules need a different tag set: " + ok.text[:80])
    ST.forget_levels()
    assert a not in [p["author_id"] for p in client.get("/federation/v1/posts").json()["posts"]], "a Newcomer's pointer stays home"
    _evidence_of(monkeypatch, a, {"op1": 1.0, "op2": 1.0})
    assert a not in [p["author_id"] for p in client.get("/federation/v1/posts").json()["posts"]], "Known: still home"
    _evidence_of(monkeypatch, a, {f"op{i}": 1.0 for i in range(5)})
    assert a in [p["author_id"] for p in client.get("/federation/v1/posts").json()["posts"]]
    monkeypatch.setenv("EMPORIA_ALLOWANCE_POINTER_TRAVEL_TIER", "known")
    _evidence_of(monkeypatch, a, {"op1": 1.0, "op2": 1.0})
    assert a in [p["author_id"] for p in client.get("/federation/v1/posts").json()["posts"]], "the setting moves it"


def test_new_topics_and_rooms_are_capped_per_day_together(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_ALLOWANCE_NEW_ROOMS_PER_DAY", "2,5,10")
    monkeypatch.setenv("EMPORIA_ALLOWANCE_NEWCOMER_BUDGET", "0")
    a = _agent(client, "tp")
    h = {"x-emporia-agent-id": a}
    topic = {"name": "my place", "description": "d"}
    r = client.post("/agoras/topics", json=topic, headers=h)
    assert r.status_code != 429 and r.status_code in (200, 400, 401, 403, 409, 422), r.text
    created = r.status_code == 200
    _room(client, a)
    if not created:
        _room(client, a)
    r = client.post("/agoras/topics", json={**topic, "name": "another"}, headers=h)
    assert r.status_code == 429 and "new rooms or topics" in r.text and "Newcomer" in r.text
    r = client.post("/rooms", json={"name": "x", "nonce": uuid.uuid4().hex, "signature": "00"}, headers=h)
    assert r.status_code == 429, "rooms and topics share the one count"
    _evidence_of(monkeypatch, a, {"op1": 1.0, "op2": 1.0})
    r = client.post("/agoras/topics", json={**topic, "name": "third"}, headers=h)
    assert r.status_code != 429, "Known has five a day"


def test_first_dms_to_strangers_are_capped_and_a_thread_the_other_began_is_not_counted(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_ALLOWANCE_STRANGER_DMS_PER_DAY", "2,5,10")
    monkeypatch.setenv("EMPORIA_ALLOWANCE_NEWCOMER_BUDGET", "0")
    a, b, c, d, f = (_agent(client, "dm") for _ in range(5))

    def start(frm, to):
        return client.post("/dm/start", json={"to_agent": to, "encrypted": False}, headers={"x-emporia-agent-id": frm})

    assert start(b, a).status_code == 200 and start(c, a).status_code == 200
    assert start(a, b).json()["created"] is False, "b began it: not a first DM of a's"
    assert start(a, c).json()["created"] is False
    assert start(a, d).json()["created"] and start(a, f).json()["created"]
    e = _agent(client, "dm")
    r = start(a, e)
    assert r.status_code == 429 and "first DMs to strangers" in r.text and "Newcomer" in r.text
    assert start(a, d).status_code == 200, "an existing thread is idempotent, never refused"
    _evidence_of(monkeypatch, a, {"op1": 1.0, "op2": 1.0})
    assert start(a, e).status_code == 200


def test_a_thread_records_who_began_it(client):
    a, b = _agent(client, "sb"), _agent(client, "sb")
    tid = client.post("/dm/start", json={"to_agent": b, "encrypted": False}, headers={"x-emporia-agent-id": a}).json()["thread_id"]
    with rs.DB_LOCK, rs.get_db() as conn:
        assert conn.execute("SELECT started_by FROM dm_threads WHERE thread_id = ?", (tid,)).fetchone()[0] == a


def test_waiting_challenges_and_open_games_by_tier(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_ALLOWANCE_CHALLENGES", "1,3,5")
    monkeypatch.setenv("EMPORIA_ALLOWANCE_OPEN_GAMES", "9,9,9")
    a, = _agents(client, "a")
    assert _challenge(client, a).status_code == 200
    r = _challenge(client, a)
    assert r.status_code == 400 and "at most 1" in r.text and "Newcomer" in r.text and "for Known" in r.text
    _evidence_of(monkeypatch, a, {"op1": 1.0, "op2": 1.0})
    assert _challenge(client, a).status_code == 200 and _challenge(client, a).status_code == 200
    assert "at most 3" in _challenge(client, a).text
    monkeypatch.setenv("EMPORIA_ALLOWANCE_CHALLENGES", "5,5,5")
    monkeypatch.setenv("EMPORIA_ALLOWANCE_OPEN_GAMES", "3,5,6")
    b, = _agents(client, "b")
    for _ in range(3):
        assert _challenge(client, b).status_code == 200
    r = _challenge(client, b)
    assert r.status_code == 429 and "games open" in r.text and "Newcomer" in r.text
    _evidence_of(monkeypatch, b, {"op1": 1.0, "op2": 1.0})
    assert _challenge(client, b).status_code == 200, "Known may keep five open"


def test_the_newcomer_budget_is_shared_by_all_newcomers_together(client, monkeypatch):
    monkeypatch.setattr(ST, "NEWCOMER_FLOOR", 0)      # the pooled budget alone; the floor has its own test
    monkeypatch.setenv("EMPORIA_ALLOWANCE_POSTS_PER_DAY", "50,50,50")
    monkeypatch.setenv("EMPORIA_ALLOWANCE_NEWCOMER_BUDGET", "100000")
    a, b, k = _agent(client, "bu"), _agent(client, "bu"), _agent(client, "bu")
    with rs.DB_LOCK, rs.get_db() as conn:
        base = ST.newcomer_actions_today(conn)
    monkeypatch.setenv("EMPORIA_ALLOWANCE_NEWCOMER_BUDGET", str(base + 3))
    assert _post(client, a).status_code == 200 and _post(client, b).status_code == 200 and _post(client, a).status_code == 200
    r = _post(client, b)
    assert r.status_code == 429 and "all Newcomers" in r.text and f"budget of {base + 3}" in r.text
    _evidence_of(monkeypatch, k, {"op1": 1.0, "op2": 1.0})
    assert _post(client, k).status_code == 200, "Known is not held by the Newcomers' budget"
    monkeypatch.setenv("EMPORIA_ALLOWANCE_NEWCOMER_BUDGET", "0")
    assert _post(client, b).status_code == 200, "0: no budget"


def test_many_fresh_labels_do_not_multiply_the_newcomer_allowance(client, monkeypatch):
    monkeypatch.setattr(ST, "NEWCOMER_FLOOR", 0)      # the pooled budget alone; the floor has its own test
    monkeypatch.setenv("EMPORIA_ALLOWANCE_POSTS_PER_DAY", "50,50,50")
    monkeypatch.setenv("EMPORIA_ALLOWANCE_NEWCOMER_BUDGET", "100000")
    agents = [_agent(client, "ml") for _ in range(4)]
    with rs.DB_LOCK, rs.get_db() as conn:
        base = ST.newcomer_actions_today(conn)
    monkeypatch.setenv("EMPORIA_ALLOWANCE_NEWCOMER_BUDGET", str(base + 4))
    done = sum(_post(client, who).status_code == 200 for who in agents for _ in range(3))
    assert done == 4, "four labels, three tries each: the day's shared budget, not twelve"


def test_the_sheet_and_the_limits_endpoint_show_tier_source_left_today_and_what_raises_it(client, monkeypatch):
    from tests.test_emporia import bearer_for
    monkeypatch.setenv("EMPORIA_ALLOWANCE_NEWCOMER_BUDGET", "0")
    a = _agent(client, "sh")
    assert _post(client, a).status_code == 200
    sheet = client.get(f"/agents/{a}/sheet").json()
    al = sheet["allowance"]
    assert al["name"] == "Newcomer" and al["source"] == "earned" and al["raises"] and "limits" not in al
    assert "Allowance" in P.render(sheet)
    h = bearer_for(client, a)
    mine = client.get("/me/allowance", headers=h).json()
    assert mine["label"] == "Newcomer" and mine["limits"]["posts_per_day"]["used"] == 1
    cap = mine["limits"]["posts_per_day"]["cap"]
    assert mine["limits"]["posts_per_day"]["left"] == cap - 1 and mine["raises"]
    assert set(mine["limits"]) == set(A.KINDS)
    other = _agent(client, "sh")
    assert client.get("/me/allowance").status_code == 401
    theirs = client.get("/me/allowance", headers=bearer_for(client, other)).json()
    assert theirs["limits"]["posts_per_day"]["used"] == 0, "each agent reads only its own, never named by the request"
    assert client.get(f"/agents/{other}/allowance", headers=h).status_code == 404
    _register_keyed(client, "sh_vouch_" + a[-4:])
    v = "sh_vouch_" + a[-4:]
    monkeypatch.setenv("EMPORIA_VOUCHED_STABLE_IDS", stable_of(v))
    ST.forget_levels()
    assert client.get(f"/agents/{v}/sheet").json()["allowance"]["label"] == "Known (vouched by the relay)"


def test_the_limits_view_reads_the_relay_allowance(monkeypatch):
    from emporia import limits
    seen = {}

    def fake(method, url, **kw):
        seen["url"] = url

        class R:
            status_code = 200

            def json(self):
                return {"label": "Newcomer", "limits": {}}
        return R()
    from emporia import mcp_server as m
    monkeypatch.setattr(m, "_bearer_request", fake)
    monkeypatch.setattr(m, "_base", lambda: "http://relay")
    out = limits.allowance("alice")
    assert seen["url"].endswith("/me/allowance") and out["label"] == "Newcomer"
    monkeypatch.setattr(m, "_bearer_request", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert "not readable" in limits.allowance("alice")["error"]
    assert "allowance" in limits.snapshot("alice")


def test_the_health_card_and_knobs_say_the_allowance_is_on(client):
    h = client.get("/health").json()
    assert h["allowance"] is True and "starter_limits" not in h
    json.dumps(client.get("/relay/card").json()["relay_modules"]["gamify"]["knobs"]["allowance"])


def test_the_schema_has_the_dm_started_by_column_and_schema_upgrade_adds_it(tmp_path):
    """Schema 57 adds one nullable column, `dm_threads.started_by` (who began a thread, for the first-DM cap): an
    older database keeps its threads and `emporia relay upgrade` adds the column, empty."""
    import sqlite3

    from emporia import schema_upgrade as U
    from emporia.schema import SCHEMA_VERSION
    from tests.test_schema_upgrade import _fill, make_old
    assert SCHEMA_VERSION >= 57
    old = make_old(tmp_path / "emporia.sqlite3")
    conn = sqlite3.connect(old)
    _fill(conn, "dm_threads", thread_id="t1", agent_a="alice", agent_b="bob", created_at="2026-11-03T00:00:00+00:00",
          last_message_at="2026-11-03T00:00:00+00:00")
    conn.commit()
    conn.close()
    assert any("started_by" in line for line in U.dry_run(old).lines())
    U.upgrade(old)
    conn = sqlite3.connect(old)
    try:
        assert conn.execute("SELECT thread_id, started_by FROM dm_threads").fetchall() == [("t1", None)]
    finally:
        conn.close()


def test_a_relay_that_still_sets_the_old_names_is_told_once_at_startup(monkeypatch, capsys):
    import asyncio

    monkeypatch.setenv("EMPORIA_STARTER_POSTS_PER_DAY", "5,6,7")

    async def start():
        async with rs.lifespan(rs.app):
            pass
    asyncio.run(start())
    err = capsys.readouterr().err
    assert err.count("EMPORIA_STARTER_POSTS_PER_DAY is replaced and now ignored") == 1
    assert "EMPORIA_ALLOWANCE_NEWCOMER_BUDGET" in err
    monkeypatch.delenv("EMPORIA_STARTER_POSTS_PER_DAY")
    assert A.retired_settings() == []


def test_one_operators_many_newcomers_cannot_spend_the_budget_for_another(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_ALLOWANCE_POSTS_PER_DAY", "50,50,50")
    monkeypatch.setenv("EMPORIA_ALLOWANCE_NEWCOMER_BUDGET", "100000")
    tag = uuid.uuid4().hex[:5]
    flood = [_agent(client, f"fl{tag}") for _ in range(4)]
    late = _agent(client, f"la{tag}")
    with rs.DB_LOCK, rs.get_db() as conn:
        base = ST.newcomer_actions_today(conn)
    monkeypatch.setenv("EMPORIA_ALLOWANCE_NEWCOMER_BUDGET", str(base + 4))
    for a in flood:                       # fresh labels each spend their floor, then the budget, until it is gone
        for _ in range(ST.NEWCOMER_FLOOR + 1):
            _post(client, a)
    assert _post(client, flood[0]).status_code == 429
    assert _post(client, late).status_code == 200, "a Newcomer that has taken nothing is not shut out by the flood"
