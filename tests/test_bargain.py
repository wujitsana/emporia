"""Bargain (`emporia:bargain:v1`): a negotiation set play with sealed priorities.

The rules, the hidden deal (verifiable, unbiasable, never leaked before the end), the
score and the Craft rating, the house's exclusion and its honest policy."""
import asyncio
import copy
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import house as H
from emporia import identity, progression, wire
from emporia import relay_server as rs
from emporia.module_sdk import (
    SessionAction,
    SessionState,
    check_deal,
    conceal,
    deal_commitment,
    deal_seed,
    get_interaction_module,
    with_deal,
)
from emporia.modules import bargain as B
from emporia.relay_server import app
from tests.test_emporia import _register_keyed, _signed_action_body

BARGAIN = "emporia:bargain:v1"
POOL = (3, 2, 1)
WHY = "Reading what they asked for and what they gave up"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51231))


def _hdr(agent):
    return {"x-emporia-agent-id": agent}


def _game(client, config=None):
    a, b = f"ga_{uuid.uuid4().hex[:8]}", f"gb_{uuid.uuid4().hex[:8]}"
    _register_keyed(client, a)
    _register_keyed(client, b)
    r = client.post("/sessions", json={"module_type": BARGAIN, "config": config or {}}, headers=_hdr(a))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    r = client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers=_hdr(b))
    assert r.status_code == 200, r.text
    return sid, a, b


def _act(client, sid, who, action, payload=None, say=""):
    return client.post(f"/sessions/{sid}/action",
                       json=_signed_action_body(client, sid, who, action, payload or {}, WHY, say=say))


def _values(client, sid, who):
    return client.get(f"/sessions/{sid}", headers=_hdr(who)).json()["private"]["values"]


def _expire(sid, seconds_late=1):
    s = rs.load_session(sid)
    state = s["state"]
    state["data"]["turn_started_at"] = (datetime.now(UTC) - timedelta(
        seconds=state["data"]["move_time_limit_s"] + seconds_late)).isoformat()
    rs.save_session(session_id=sid, module_type=BARGAIN, config=s["config"], payment_rules=s["payment_rules"],
                    state=SessionState.from_dict(state), current_agent=s["current_agent"],
                    step_number=s["step_number"], status="active", participants=s["participants"],
                    created_at=s["created_at"], updated_at=datetime.now(UTC).isoformat())


# ── the deal: fair by construction ───────────────────────────────────────────

def test_every_possible_deal_is_fair_and_neither_seat_is_favoured():
    pairs = B.deal_pairs(POOL)
    assert pairs and set(pairs) == {(b, a) for a, b in pairs}, "symmetric: no seat is dealt better hands"
    for a, b in pairs:
        assert B.worth(a, POOL) == B.worth(b, POOL) == B.TOTAL, "the whole pool is worth the same to both"
        assert a != b, "the priorities differ"
        assert any(2 * B.worth(a, k) > B.TOTAL and 2 * B.worth(b, B.rest(POOL, k)) > B.TOTAL
                   for k in B.bundles(POOL)), "a real gain from trade: some split gives both more than half"
        assert all(sum(x > 0 for x in v) >= B.MIN_GOODS_VALUED for v in (a, b))


def test_a_pool_with_no_fair_deal_is_refused():
    mod = get_interaction_module(BARGAIN)
    assert mod.validate_config({})[0]
    assert not mod.validate_config({"silk": 4, "spice": 4, "amber": 4})[0], "4·a+4·b+4·c is never 10"
    for bad in ({"max_offers": 1}, {"walk_away": 5}, {"silk": 0}, {"move_time_limit_s": 5}, {"silk": "3"}):
        assert not mod.validate_config(bad)[0], bad


def test_the_seed_needs_every_share_and_each_share_changes_the_deal():
    relay_seed, shares = "ab" * 32, ["sig-creator", "sig-joiner"]
    seed = deal_seed(relay_seed, shares)
    assert deal_seed(relay_seed, shares) == seed, "deterministic: anyone can redo it"
    assert seed != deal_seed("cd" * 32, shares), "the relay's secret matters"
    assert seed != deal_seed(relay_seed, ["sig-creator", "other"]), "the joiner's share matters"
    assert seed != deal_seed(relay_seed, ["other", "sig-joiner"]), "the creator's share matters"
    # Over many joiner shares the deals spread over the whole set: no share is special.
    dealt = {B.deal_values(deal_seed(relay_seed, ["c", f"j{i}"]), POOL) for i in range(4000)}
    assert len(dealt) == len(B.deal_pairs(POOL))


def test_a_joiner_has_exactly_one_share_so_the_relay_cannot_ask_for_another():
    """The joiner's share signs the commitment and nothing it chooses (no time, no nonce):
    Ed25519 is deterministic, so a relay that refuses a join gets the same share again."""
    agent = f"gj_{uuid.uuid4().hex[:6]}"
    payload = wire.signed_deal_seed_payload("sess_x", agent, deal_commitment("ab" * 32))
    assert identity.sign(payload, agent) == identity.sign(payload, agent)
    assert set(payload) == {"type", "session_id", "agent_id", "commitment"}


def test_the_relay_commits_before_anyone_joins_and_the_deal_checks_out_after(client):
    sid, a, b = _game(client)
    s = rs.load_session(sid)
    commitment = s["config"]["deal_commitment"]
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT * FROM session_deals WHERE session_id = ?", (sid,)).fetchone()
    assert deal_commitment(row["relay_seed"]) == commitment
    contrib = s["state"]["data"]["deal"]["contributions"]
    assert contrib[a]["kind"] == "session-create" and contrib[b]["kind"] == "deal-seed"
    assert identity.verify(wire.signed_deal_seed_payload(sid, b, commitment), contrib[b]["signature"],
                           identity.get_public_key_hex(b)), "the joiner's share binds the commitment it saw"
    assert _act(client, sid, a, "offer", {"keep": {"silk": 3}}).status_code == 200
    assert _act(client, sid, b, "accept").status_code == 200
    s = rs.load_session(sid)
    assert s["status"] == "completed"
    deal = s["state"]["data"]["deal"]
    assert deal["relay_seed"] == row["relay_seed"], "published at the end"
    assert check_deal(get_interaction_module(BARGAIN), s["participants"], s["config"], deal) == ""
    forged = {**deal, "hands": {a: deal["hands"][b], b: deal["hands"][a]}}
    assert check_deal(get_interaction_module(BARGAIN), s["participants"], s["config"], forged), \
        "swapped hands do not pass"


def test_a_creator_cannot_set_the_commitment_and_a_join_without_its_share_is_refused(client):
    a, b = f"gc_{uuid.uuid4().hex[:6]}", f"gd_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, a)
    _register_keyed(client, b)
    r = client.post("/sessions", json={"module_type": BARGAIN, "config": {"deal_commitment": "0" * 64}},
                    headers=_hdr(a))
    assert r.status_code == 400 and "relay's" in r.text
    sid = client.post("/sessions", json={"module_type": BARGAIN}, headers=_hdr(a)).json()["session_id"]
    r = client.post(f"/sessions/{sid}/join", json={"agent_id": b, "deal_signature": ""}, headers=_hdr(b))
    assert r.status_code == 400 and "deal_signature" in r.text
    other = wire.signed_deal_seed_payload(sid, b, deal_commitment("ff" * 32))
    r = client.post(f"/sessions/{sid}/join", json={"agent_id": b, "deal_signature": identity.sign(other, b)},
                    headers=_hdr(b))
    assert r.status_code == 403, "a share over another commitment does not verify"
    assert rs.load_session(sid)["status"] == "waiting" and rs.load_session(sid)["participants"] == [a]


# ── private information stays private until the end ──────────────────────────

def _everything_served_to(client, sid, viewer):
    out = [client.get(f"/sessions/{sid}", headers=_hdr(viewer)).json(),
           client.get(f"/sessions/{sid}/state", headers=_hdr(viewer)).json(),
           client.get(f"/sessions/{sid}/actions", headers=_hdr(viewer)).json(),
           client.get(f"/sessions/{sid}/audit", headers=_hdr(viewer)).json(),
           client.get(f"/sessions/{sid}/chat", headers=_hdr(viewer)).json(),
           [x for x in client.get("/sessions", params={"module_type": BARGAIN}, headers=_hdr(viewer)).json()
            ["sessions"] if x["session_id"] == sid]]
    return json.dumps(out)


def test_no_one_sees_the_other_hand_or_the_seed_before_the_end(client, monkeypatch):
    sent = []

    async def capture(*args):
        sent.append(json.dumps(args[-1], default=str))
    for name in ("broadcast_to_session", "broadcast_to_agent", "broadcast_global"):
        monkeypatch.setattr(rs, name, capture)
    sid, a, b = _game(client)
    with rs.DB_LOCK, rs.get_db() as conn:
        relay_seed = conn.execute("SELECT relay_seed FROM session_deals WHERE session_id = ?", (sid,)).fetchone()[0]
    va, vb = _values(client, sid, a), _values(client, sid, b)
    assert va != vb
    mine = client.get(f"/sessions/{sid}", headers=_hdr(a)).json()
    assert mine["private"]["values"] == va and mine["state"]["data"]["deal"]["hands"] is None
    assert "private" not in client.get(f"/sessions/{sid}", headers=_hdr(f"spectator_{uuid.uuid4().hex[:4]}")).json()
    for step, (who, action, payload) in enumerate([(a, "offer", {"keep": {"silk": 2, "spice": 1}}),
                                                   (b, "offer", {"keep": {"amber": 1, "spice": 2},
                                                                 "claim": {"silk": 0, "spice": 4, "amber": 2}}),
                                                   (a, "offer", {"keep": {"silk": 3, "spice": 1}})]):
        r = _act(client, sid, who, action, payload, say="my turn" if step == 0 else "")
        assert r.status_code == 200, r.text
        assert relay_seed not in r.text and "hands\": {" not in r.text
        for viewer in (a, b, "outsider"):
            served = _everything_served_to(client, sid, viewer)
            assert relay_seed not in served, "the relay's seed is never served during play"
            assert '"hands": {' not in served and '"seed": "' not in served
        assert json.loads(_everything_served_to(client, sid, a))[0]["private"]["values"] == va
        assert json.loads(_everything_served_to(client, sid, b))[0]["private"]["values"] == vb
    stored = json.dumps(rs.load_session(sid)) + json.dumps(client.get(f"/sessions/{sid}/actions",
                                                                      headers=_hdr(a)).json())
    assert relay_seed not in stored and '"hands": {' not in stored
    assert "revealed" not in client.get(f"/sessions/{sid}/chat", headers=_hdr(a)).json()
    assert sent and not any(relay_seed in e or '"hands": {' in e for e in sent), "nor in any event pushed out"
    assert _act(client, sid, b, "accept").status_code == 200
    chat = client.get(f"/sessions/{sid}/chat", headers=_hdr("outsider")).json()
    assert chat["revealed"]["hands"] == {a: {"values": va}, b: {"values": vb}}, "beside the talk, after the end"
    assert [m["content"] for m in chat["messages"]] == ["my turn"]
    assert len(chat["revealed"]["offers"]) == 3


def test_the_chat_is_on_by_default_and_can_be_turned_off(client):
    sid, a, _ = _game(client)
    assert rs.load_session(sid)["config"]["chat"] is True
    sid2, _, _ = _game(client, {"chat": False})
    assert rs.load_session(sid2)["config"]["chat"] is False


# ── the rules ────────────────────────────────────────────────────────────────

def test_legal_and_illegal_offers(client):
    sid, a, b = _game(client)
    for bad, why in (({"keep": ["silk"]}, "names goods"), ({"keep": 3}, "names goods"), ({}, "names goods"),
                     ({"keep": {"silk": 4}}, "0–3"), ({"keep": {"gold": 1}}, "names goods"),
                     ({"keep": {"silk": -1}}, "0–3"), ({"keep": {"silk": True}}, "0–3"),
                     ({"keep": {}, "claim": {"silk": 11}}, "claim"), ({"keep": {}, "extra": 1}, "an offer is")):
        r = _act(client, sid, a, "offer", bad)
        assert r.status_code == 400 and why in r.text, (bad, r.text)
    assert _act(client, sid, a, "accept").status_code == 400, "nothing on the table yet"
    assert _act(client, sid, a, "decline").status_code == 400
    assert _act(client, sid, b, "offer", {"keep": {}}).status_code == 400, "the creator opens"
    assert _act(client, sid, a, "offer", {"keep": {}}).status_code == 200, "keeping nothing is an offer"
    assert _act(client, sid, a, "offer", {"keep": {}}).status_code == 400, "not twice in a row"
    assert _act(client, sid, b, "decline").status_code == 400, "decline is for the last offer only"
    assert _act(client, sid, b, "bid", {}).status_code == 400


def test_an_accepted_offer_scores_each_side_its_own_values(client):
    sid, a, b = _game(client)
    va, vb = _values(client, sid, a), _values(client, sid, b)
    keep = {"silk": 2, "spice": 1, "amber": 0}
    _act(client, sid, a, "offer", {"keep": keep})
    r = _act(client, sid, b, "accept")
    out = r.json()["outcome"]
    gets_b = {"silk": 1, "spice": 1, "amber": 1}
    pa = sum(va[g] * keep[g] for g in B.GOODS)
    pb = sum(vb[g] * gets_b[g] for g in B.GOODS)
    assert out["termination"] == "deal" and out["points"] == {a: pa, b: pb}
    assert out["rating_scores"] == {a: pa / 10, b: pb / 10}
    assert out["winner"] == (a if pa > pb else b if pb > pa else None)
    assert out["allocation"] == {a: keep, b: gets_b} and out["values"] == {a: va, b: vb}
    assert _act(client, sid, a, "offer", {"keep": {}}).status_code == 400, "over"
    result = next(x for x in client.get("/results", params={"module_type": BARGAIN}).json()["results"]
                  if x["session_id"] == sid)
    assert result["points"] == {a: pa, b: pb} and result["record"]["deal"]["hands"][a]["values"] == va


def test_the_last_offer_can_only_be_accepted_or_declined_and_declining_is_no_deal(client):
    sid, a, b = _game(client, {"max_offers": 2, "walk_away": 1})
    _act(client, sid, a, "offer", {"keep": {"silk": 3, "spice": 2, "amber": 1}})
    _act(client, sid, b, "offer", {"keep": {"silk": 3, "spice": 2, "amber": 1}, "claim": {"silk": 3, "amber": 1}})
    r = _act(client, sid, a, "offer", {"keep": {}})
    assert r.status_code == 400 and "no offers are left" in r.text
    out = _act(client, sid, a, "decline").json()["outcome"]
    assert out["termination"] == "no_deal" and out["winner"] is None and out["outcome_type"] == "draw"
    assert out["points"] == {a: 1, b: 1} and out["rating_scores"] == {a: 0.1, b: 0.1}
    claims = out["claims"]
    assert set(claims) == {b} and claims[b]["claimed"] == {"silk": 3, "spice": 0, "amber": 1}
    assert claims[b]["matches"] == (claims[b]["actual"] == claims[b]["claimed"]), "said beside dealt, no penalty"


def test_running_out_of_time_loses_and_scores_zero(client):
    sid, a, b = _game(client, {"move_time_limit_s": 60, "walk_away": 2})
    _act(client, sid, a, "offer", {"keep": {"silk": 1}})
    _expire(sid)
    assert _act(client, sid, b, "accept").status_code == 400, "too late"
    from emporia.routes.sessions import sweep_service_deadlines
    assert asyncio.run(sweep_service_deadlines())["game_timeouts"] >= 1
    s = rs.load_session(sid)
    assert s["status"] == "completed"
    _, out = get_interaction_module(BARGAIN).is_terminal(SessionState.from_dict(s["state"]))
    assert out["winner"] == a and out["termination"] == "timeout" and out["points"] == {b: 0, a: 2}
    assert "rating_scores" not in out, "a loss on time is a plain loss, as in every set play"
    assert out["values"][a] and s["state"]["data"]["deal"]["relay_seed"], "the deal is published on time too"
    assert progression._scores({"participants": [a, b], **out}) == {(a, b): 1.0, (b, a): 0.0}


def test_an_opener_who_never_opens_is_a_draw_and_no_result(client):
    sid, a, b = _game(client, {"move_time_limit_s": 60})
    _expire(sid)
    from emporia.routes.sessions import sweep_service_deadlines
    asyncio.run(sweep_service_deadlines())
    s = rs.load_session(sid)
    _, out = get_interaction_module(BARGAIN).is_terminal(SessionState.from_dict(s["state"]))
    assert s["status"] == "completed" and out["winner"] is None and out["termination"] == "timeout"
    assert progression._scores({"participants": [a, b], **out}) is None


def test_no_stakes_and_no_bounty_on_a_game_whose_relay_holds_the_hands(client):
    from emporia.bounties import judging_modules
    from emporia.module_sdk import MODULE_REGISTRY, list_available_modules
    assert BARGAIN not in judging_modules(MODULE_REGISTRY)
    a = f"gs_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, a)
    r = client.post("/sessions", json={"module_type": BARGAIN, "payment_rules": {
        "mode": "x402", "stake_per_participant": "1.00"}}, headers=_hdr(a))
    assert r.status_code == 400 and "takes no stake" in r.text
    listed = next(m for m in list_available_modules() if m["module_type"] == BARGAIN)
    assert listed["stakes_allowed"] is False and listed["engine_decidable"] is False
    assert "private.values" in listed["rules"]


def test_verify_session_checks_the_deal_and_catches_forged_hands(client):
    from emporia.verify import verify_game
    sid, a, b = _game(client)
    _act(client, sid, a, "offer", {"keep": {"silk": 2, "amber": 1}})
    _act(client, sid, b, "accept")
    record = {
        "session": client.get(f"/sessions/{sid}", headers=_hdr(a)).json(),
        "log": client.get(f"/sessions/{sid}/audit", headers=_hdr(a)).json()["chain"],
        "public_keys": {x: client.get(f"/agents/{x}", headers=_hdr(a)).json()["public_key_hex"] for x in (a, b)},
        "receipts": [], "relay_key": client.get("/health").json()["relay_public_key"],
        "relay_verdict": next(r for r in client.get("/results", params={"agent_id": a}).json()["results"]
                              if r["session_id"] == sid),
    }
    report = verify_game(**record)
    deal = next(f for f in report["findings"] if f["check"] == "deal")
    assert deal["status"] == "ok", deal
    assert next(f for f in report["findings"] if f["check"] == "verdict")["status"] == "ok"
    forged = copy.deepcopy(record)
    d = forged["session"]["state"]["data"]["deal"]
    d["hands"] = {a: d["hands"][b], b: d["hands"][a]}
    assert next(f for f in verify_game(**forged)["findings"] if f["check"] == "deal")["status"] == "fail"
    regrind = copy.deepcopy(record)
    regrind["session"]["state"]["data"]["deal"]["contributions"][b]["signature"] = "x" * 86
    assert next(f for f in verify_game(**regrind)["findings"] if f["check"] == "deal")["status"] == "fail"


# ── the module on its own ────────────────────────────────────────────────────

def _dealt_state(values_a=(1, 2, 3), values_b=(2, 1, 2), config=None):
    mod = get_interaction_module(BARGAIN)
    st = mod.initial_state(["a", "b"], config or {})
    hands = {"a": {"values": dict(zip(B.GOODS, values_a))}, "b": {"values": dict(zip(B.GOODS, values_b))}}
    return mod, with_deal(st, hands=hands, relay_seed="x", seed="y")


def test_conceal_strips_the_seed_and_hands_and_nothing_else():
    _, st = _dealt_state()
    hidden = conceal(st)
    assert hidden.data["deal"]["hands"] is None and hidden.data["deal"]["relay_seed"] is None
    assert st.data["deal"]["hands"], "the original is untouched"
    assert {k: v for k, v in hidden.data.items() if k != "deal"} == {k: v for k, v in st.data.items() if k != "deal"}


def test_private_view_is_the_players_own_hand_and_what_each_offer_is_worth_to_it():
    mod, st = _dealt_state()
    st = mod.apply_action(st, SessionAction("a", "offer", {"keep": {"silk": 3}})).new_state
    view = mod.private_view(st.data, "b")
    assert view["values"] == {"silk": 2, "spice": 1, "amber": 2} and view["all_goods_worth"] == 10
    assert view["offers"] == [{"n": 1, "by": "a", "worth_to_you": 1 * 2 + 1 * 2}]   # b gets 2 spice, 1 amber


# ── the rating ───────────────────────────────────────────────────────────────

def _bargain(a, b, sa, sb, at, termination="deal", winner=None):
    return {"session_id": f"s{at}", "module_type": BARGAIN, "participants": [a, b], "winner": winner,
            "termination": termination, "rating_scores": {a: sa, b: sb}, "ended_at": at}


def test_a_good_deal_raises_both_and_no_deal_lowers_both():
    good = progression.craft_ratings([_bargain("a", "b", 0.7, 0.7, 1)])[BARGAIN]
    assert good["a"].rating > progression.START_RATING and good["b"].rating > progression.START_RATING
    none = progression.craft_ratings([_bargain("a", "b", 0.0, 0.0, 1, "no_deal")])[BARGAIN]
    assert none["a"].rating < progression.START_RATING and none["b"].rating < progression.START_RATING
    even = progression.craft_ratings([_bargain("a", "b", 0.5, 0.5, 1)])[BARGAIN]
    assert round(even["a"].rating) == round(progression.START_RATING), "half the pool each: as expected, no change"
    lopsided = progression.craft_ratings([_bargain("a", "b", 0.9, 0.2, 1, winner="a")])[BARGAIN]
    assert lopsided["a"].rating > progression.START_RATING > lopsided["b"].rating


def test_same_operator_and_repeat_bargains_count_as_the_other_set_plays():
    who = {"a": progression.Who("op1", True, 0), "sock": progression.Who("op1", True, 0),
           "b": progression.Who("op2", True, 0)}
    assert BARGAIN not in progression.craft_ratings([_bargain("a", "sock", 1.0, 1.0, 1)], who), \
        "your own other agent is not an opponent"
    once = progression.craft_ratings([_bargain("a", "b", 0.8, 0.8, 1)], who)[BARGAIN]["a"].rating
    many = progression.craft_ratings([_bargain("a", "b", 0.8, 0.8, i) for i in range(1, 11)], who)[BARGAIN]["a"].rating
    assert many - progression.START_RATING < 10 * (once - progression.START_RATING), "repeats flatten (1/√n)"


def test_a_house_bargain_counts_for_no_rating_but_does_for_quests(client, monkeypatch):
    from emporia import quests
    from emporia.routes import standing
    sid, a, h = _game(client)
    monkeypatch.setenv("EMPORIA_HOUSE_AGENTS", h)
    _act(client, sid, a, "offer", {"keep": {"silk": 1}})
    _act(client, sid, h, "accept")
    standing._TABLE_CACHE.update(key=None, value=None)
    assert sid not in {g["session_id"] for g in standing._craft_games()}, "practice with the house counts nothing"
    games = standing._public_games_of(a)
    assert any(g["session_id"] == sid and g["termination"] == "deal" for g in games)
    done = {q["id"] for q in quests.progress(a, posts=[], games=games, deals=[], reports=[])["quests"] if q["done"]}
    assert {"first-set-play", "bargain"} <= done, "XP only: the house counts for quests"


def test_strike_a_bargain_needs_a_deal_and_is_never_next():
    from emporia import quests
    g = {"session_id": "s", "module_type": BARGAIN, "participants": ["me", "x"], "ended_at": "2026-11-05"}
    none = quests.progress("me", posts=[], games=[{**g, "termination": "no_deal"}], deals=[], reports=[])
    assert not next(q for q in none["quests"] if q["id"] == "bargain")["done"]
    assert "bargain" not in none["next"]
    deal = quests.progress("me", posts=[], games=[{**g, "termination": "deal"}], deals=[], reports=[])
    q = next(q for q in deal["quests"] if q["id"] == "bargain")
    assert q["done"] and q["optional"] and deal["xp"] == 10 + 10


def test_bargain_counts_for_craft_and_is_not_engine_decided():
    from emporia.routes.standing import craft_modules
    assert BARGAIN in craft_modules()
    assert get_interaction_module(BARGAIN).ENGINE_DECIDABLE is False


# ── the house's honest policy ────────────────────────────────────────────────

def test_the_house_offers_about_half_by_its_own_values_and_says_them():
    pool = {"silk": 3, "spice": 2, "amber": 1}
    for values in B.value_vectors(POOL):
        v = dict(zip(B.GOODS, values))
        keep = H.bargain_keep(v, pool, 10)
        w = H._worth(v, keep)
        assert 5 <= w and all(5 > H._worth(v, dict(zip(B.GOODS, k))) or H._worth(v, dict(zip(B.GOODS, k))) >= w
                              for k in B.bundles(POOL)), "the least bundle worth half or more"
    session = {"config": {"chat": True}, "private": {"values": {"silk": 2, "spice": 1, "amber": 2}},
               "state": {"data": {"pool": pool, "total": 10, "max_offers": 6, "walk_away": 0, "offers": []}}}
    action, payload, said = H.bargain_move(session, "house")
    assert action == "offer" and payload["claim"] == {"silk": 2, "spice": 1, "amber": 2}, "it claims the truth"
    assert "silk 2, spice 1, amber 2" in said
    assert H.bargain_move({**session, "private": {}}, "house") is None, "no hand: no move"


def test_the_house_accepts_half_or_better_and_any_deal_at_the_end():
    assert H.bargain_accepts(5, 10, False, 0) and not H.bargain_accepts(4, 10, False, 0)
    assert H.bargain_accepts(1, 10, True, 0) and not H.bargain_accepts(1, 10, True, 1)
    pool = {"silk": 3, "spice": 2, "amber": 1}
    offer = {"n": 1, "by": "x", "allocation": {"x": {"silk": 3, "spice": 0, "amber": 0},
                                               "house": {"silk": 0, "spice": 2, "amber": 1}}}
    s = {"config": {}, "private": {"values": {"silk": 2, "spice": 1, "amber": 2}},
         "state": {"data": {"pool": pool, "total": 10, "max_offers": 6, "walk_away": 0, "offers": [offer]}}}
    assert H.bargain_move(s, "house")[0] == "offer", "worth 4 to it: it counters"
    last = copy.deepcopy(s)
    last["state"]["data"]["max_offers"] = 1
    assert H.bargain_move(last, "house")[0] == "accept", "the last offer: 4 beats nothing"
    last["state"]["data"]["walk_away"] = 4
    assert H.bargain_move(last, "house")[0] == "decline"


def test_the_house_knows_its_turn_and_adopts_a_waiting_bargain():
    now = datetime.now(UTC).timestamp()
    s = {"session_id": "s1", "module_type": BARGAIN, "status": "active", "participants": ["house", "x"],
         "current_agent": "house", "state": {"current_agent": "house", "data": {"ended": None}},
         "created_at": (datetime.now(UTC) - timedelta(seconds=300)).isoformat(), "payment_rules": {}}
    assert H.my_turn(s, "house") == "bargain"
    assert H.my_turn({**s, "current_agent": "x", "state": {"current_agent": "x", "data": {}}}, "house") == ""
    assert H.adoptable({**s, "status": "waiting", "participants": ["x"]}, "house", now, 120, False)


def test_the_house_bargains_a_whole_game_through_the_relay(client):
    """The house's policy against the relay's real game: each turn it reads its own hand
    (get_session's `private`) and moves; a newcomer who asks for half by the house's
    stated values gets a deal."""
    sid, newcomer, house = _game(client)
    hdr = _hdr(house)
    _act(client, sid, newcomer, "offer", {"keep": {"silk": 3, "spice": 2, "amber": 1}}, say="all mine?")
    for _ in range(6):
        s = client.get(f"/sessions/{sid}", headers=hdr).json()
        if s["status"] != "active":
            break
        if s["current_agent"] == house:
            action, payload, said = H.bargain_move(s, house)
            assert _act(client, sid, house, action, payload, say=said).status_code == 200
        else:   # the newcomer accepts the house's honest offer
            assert _act(client, sid, newcomer, "accept").status_code == 200
    out = get_interaction_module(BARGAIN).is_terminal(SessionState.from_dict(rs.load_session(sid)["state"]))[1]
    assert out["termination"] == "deal" and out["points"][house] >= 5
    chat = client.get(f"/sessions/{sid}/chat", headers=hdr).json()
    assert any("Honest house" in m["content"] for m in chat["messages"])
    vals = chat["revealed"]["hands"][house]["values"]
    assert all(str(vals[g]) in m["content"] for m in chat["messages"] if "Honest house" in m["content"]
               for g in B.GOODS), "what it said is what it was dealt"


def test_the_mcp_tools_refuse_an_illegal_offer_before_signing_and_join_with_a_share(monkeypatch):
    from emporia import mcp_server as m

    class R:
        def __init__(self, body, code=200):
            self._b, self.status_code = body, code

        def json(self):
            return self._b

        def raise_for_status(self):
            pass

    sess = {"session_id": "sess_t", "module_type": BARGAIN, "step_number": 1, "status": "active",
            "config": {"deal_commitment": "ab" * 32}, "payment_rules": {},
            "state": {"data": {"pool": {"silk": 3, "spice": 2, "amber": 1}, "offers": [], "max_offers": 6}}}
    posted = []

    def fake(method, url, **kw):
        if method == "post":
            posted.append((url, kw.get("json")))
            return R({"session_id": "sess_t"})
        return R(sess)
    monkeypatch.setattr(m, "_bearer_request", fake)
    me = f"gm_{uuid.uuid4().hex[:6]}"
    identity.get_public_key_hex(me)
    r = m.submit_action("sess_t", "offer", {"keep": {"silk": 9}}, WHY, agent_id=me)
    assert not r["success"] and "0–3" in r["error"] and not posted, "nothing signed, nothing sent"
    m.join_session("sess_t", agent_id=me)
    body = posted[-1][1]
    assert identity.verify(wire.signed_deal_seed_payload("sess_t", me, "ab" * 32), body["deal_signature"],
                           identity.get_public_key_hex(me))


def test_a_v32_relay_is_copied_aside_then_given_the_deals_table(tmp_path, monkeypatch):
    import sqlite3
    db = tmp_path / "relay.sqlite3"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE kept_from_v32 (x TEXT)")
    old.execute("PRAGMA user_version = 32")
    old.commit()
    old.close()
    monkeypatch.setattr(rs, "DATABASE_PATH", db)
    rs.init_db()
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == rs.SCHEMA_VERSION >= 33
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'session_deals'").fetchone()
    [snap] = list(tmp_path.glob(f"relay.sqlite3.before-v{rs.SCHEMA_VERSION}-*.bak"))
    assert sqlite3.connect(snap).execute("PRAGMA user_version").fetchone()[0] == 32
