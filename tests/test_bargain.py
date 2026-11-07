"""Bargain v2 (`emporia:bargain:v2`): a negotiation set play whose hands nobody deals.

Each player commits to a secret with its create/join; its hand and walk-away come from that secret and
a public drand round (`emporia:rand:v1`). The relay holds neither. The tests use the shim's fake
beacon (conftest `rand_testmode`); the real BLS check has its own tests in test_rand.py."""
from tests.ids import stable_of  # noqa: E402
import asyncio
import copy
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import progression, rand
from emporia import relay_server as rs
from emporia.module_sdk import SessionState, get_interaction_module
from emporia.modules import bargain as B
from emporia.relay_server import app
from conftest import RAND_SECRETS, fake_beacon_sig, make_test_commit
from tests.test_emporia import _register_keyed, _signed_action_body

BARGAIN = "emporia:bargain:v2"
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


def _hand(sid, who):
    """What the player's own client computes: (hand, walk-away) from its secret and the round."""
    s = rs.load_session(sid)
    r = s["state"]["data"]["rand"]
    seat = s["participants"].index(who)
    return B.draw(r["scope"], seat, bytes.fromhex(fake_beacon_sig(r["round"])),
                  bytes.fromhex(RAND_SECRETS[(sid, who)][0]))


def _reveal(client, sid, who):
    s, nonce = RAND_SECRETS[(sid, who)]
    return _act(client, sid, who, "reveal", {"s": s, "nonce": nonce})


def _play_deal(client, sid, a, b, keep=None):
    assert _act(client, sid, a, "offer", {"keep": keep or {"silk": 2, "amber": 1}}).status_code == 200
    assert _act(client, sid, b, "accept").status_code == 200


def _expire(sid, seconds_late=1, field="turn_started_at", window=None):
    s = rs.load_session(sid)
    state = s["state"]
    state["data"][field] = (datetime.now(UTC) - timedelta(
        seconds=(window if window is not None else state["data"]["move_time_limit_s"]) + seconds_late)).isoformat()
    rs.save_session(session_id=sid, module_type=BARGAIN, config=s["config"], payment_rules=s["payment_rules"],
                    state=SessionState.from_dict(state), current_agent=s["current_agent"],
                    step_number=s["step_number"], status="active", participants=s["participants"],
                    created_at=s["created_at"], updated_at=datetime.now(UTC).isoformat())


def _sweep():
    from emporia.routes.sessions import sweep_service_deadlines
    asyncio.run(sweep_service_deadlines())


def _outcome(sid):
    s = rs.load_session(sid)
    return s, get_interaction_module(BARGAIN).is_terminal(SessionState.from_dict(s["state"]))[1]


# ── the hands: fair by construction ──────────────────────────────────────────

def test_the_eight_hands_every_distinct_ordered_pair_has_a_gain_from_trade():
    assert len(B.HANDS) == len(set(B.HANDS)) == 8
    for h in B.HANDS:
        assert B.worth(h, POOL) == B.TOTAL, "the whole pool is worth 10 to every hand"
    pairs = [(x, y) for x in B.HANDS for y in B.HANDS if x != y]
    assert len(pairs) == 56
    for x, y in pairs:
        assert any(2 * B.worth(x, k) > B.TOTAL and 2 * B.worth(y, B.rest(POOL, k)) > B.TOTAL
                   for k in B.bundles(POOL)), (x, y)
    assert not any(2 * B.worth(h, k) > B.TOTAL and 2 * B.worth(h, B.rest(POOL, k)) > B.TOTAL
                   for h in B.HANDS for k in B.bundles(POOL)), "identical hands never have one: that is the collision"


def test_the_draw_is_the_players_own_and_uniform_over_hands_and_walk_aways():
    sig, s = bytes.fromhex(fake_beacon_sig(7)), bytes(32)
    assert B.draw("sess_x", 0, sig, s) == B.draw("sess_x", 0, sig, s), "deterministic: anyone redoes it at the end"
    seen_h, seen_w = set(), set()
    for i in range(400):
        h, w = B.draw("sess_x", 0, sig, i.to_bytes(32, "big"))
        seen_h.add(h)
        seen_w.add(w)
    assert seen_h == set(B.HANDS) and seen_w == set(range(5))
    with pytest.raises(ValueError):
        rand.priv("sess_x", 0, "hand", b"", s)     # no beacon, no private draw: it would be the player's own choice


def test_settings_are_validated_and_the_pool_and_walk_away_are_fixed():
    mod = get_interaction_module(BARGAIN)
    assert mod.validate_config({})[0] and mod.validate_config({"silk": 3, "spice": 2, "amber": 1})[0]
    for bad in ({"max_offers": 1}, {"walk_away": 2}, {"silk": 4}, {"move_time_limit_s": 5}, {"reveal_window_s": 1}):
        assert not mod.validate_config(bad)[0], bad


def test_the_old_label_is_refused_by_name(client):
    a = f"gv1_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, a)
    r = client.post("/sessions", json={"module_type": "emporia:bargain:v1"}, headers=_hdr(a))
    assert r.status_code == 400 and "Unknown module" in r.text and "emporia:bargain:v1" in r.text


# ── the relay holds nothing private ──────────────────────────────────────────

def test_the_relays_stored_state_of_a_live_game_holds_no_hand_and_no_walk_away(client):
    sid, a, b = _game(client)
    assert _act(client, sid, a, "offer", {"keep": {"silk": 2}, "claim": {"silk": 4, "spice": 3, "amber": 0}}).status_code == 200
    hands = {p: _hand(sid, p) for p in (a, b)}
    s = rs.load_session(sid)
    assert s["status"] == "active"
    blob = json.dumps(s)
    with rs.DB_LOCK, rs.get_db() as conn:     # and nothing else the relay keeps about this game
        for table in ("sessions", "session_actions", "game_chat", "participants"):
            try:
                rows = conn.execute(f"SELECT * FROM {table} WHERE session_id = ?", (sid,)).fetchall()
            except Exception:  # noqa: BLE001
                continue
            blob += json.dumps([dict(r) for r in rows], default=str)
    for p in (a, b):
        secret = RAND_SECRETS[(sid, p)]
        assert secret[0] not in blob and secret[1] not in blob, "a secret never reaches the relay before the reveal"
    data = s["state"]["data"]
    assert "walk_away" not in data and "values" not in blob.replace('"values": {}', "")
    assert set(data["rand"]) == {"scope", "commits", "round", "beacon_sig", "reveals"}
    assert data["rand"]["reveals"] == {} and data["rand"]["beacon_sig"] is None
    assert "private" not in client.get(f"/sessions/{sid}", headers=_hdr(a)).json(), "the relay serves no view"
    assert hands[a][0] in B.HANDS


def test_a_game_needs_a_commitment_to_create_and_to_join(client):
    from datetime import UTC, datetime

    from emporia import identity, wire
    from conftest import signed_session_body
    a, b = f"gc_{uuid.uuid4().hex[:6]}", f"gd_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, a)
    _register_keyed(client, b)
    r = client.post("/sessions", json=signed_session_body(a, {"module_type": BARGAIN}), headers=_hdr(a))
    assert r.status_code == 400 and "rand_commit" in r.text
    sid = client.post("/sessions", json={"module_type": BARGAIN}, headers=_hdr(a)).json()["session_id"]
    at = datetime.now(UTC).isoformat()
    body = {"agent_id": b, "at": at, "signature": identity.sign(wire.signed_session_join_payload(sid, stable_of(b), at), b)}
    r = client.post(f"/sessions/{sid}/join", json=body, headers=_hdr(b))
    assert r.status_code == 400 and "rand_commit" in r.text
    assert rs.load_session(sid)["participants"] == [a]


def test_a_commitment_must_be_signed_by_its_player_and_name_a_round_ten_ahead(client, monkeypatch):
    from conftest import signed_session_body
    a = f"ge_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, a)
    body = signed_session_body(a, {"module_type": BARGAIN})
    from emporia import wire
    sid = wire.session_id_for(body["nonce"])
    monkeypatch.setattr(rand, "MIN_LEAD_ROUNDS", 10)
    early = make_test_commit(sid, a, 0, round_offset=3)
    r = client.post("/sessions", json={**body, "rand_commit": early}, headers=_hdr(a))
    assert r.status_code == 400 and "at least 10 rounds" in r.text
    ok = make_test_commit(sid, a, 0, round_offset=10)
    forged = {**ok, "signature": "00" * 64}
    assert client.post("/sessions", json={**body, "rand_commit": forged}, headers=_hdr(a)).status_code == 403
    wrong_seat = make_test_commit(sid, a, 1, round_offset=10)
    assert client.post("/sessions", json={**body, "rand_commit": wrong_seat}, headers=_hdr(a)).status_code == 403
    r = client.post("/sessions", json={**body, "rand_commit": ok}, headers=_hdr(a))
    assert r.status_code == 200, r.text
    assert rs.load_session(sid)["config"]["rand"]["commits"][a]["beacon_round"] == ok["beacon_round"]


def test_config_rand_is_the_relays_not_a_clients(client):
    a = f"gf_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, a)
    r = client.post("/sessions", json={"module_type": BARGAIN, "config": {"rand": {"commits": {}}}}, headers=_hdr(a))
    assert r.status_code == 400 and "config.rand" in r.text


# ── playing ──────────────────────────────────────────────────────────────────

def test_the_chat_is_on_by_default_and_can_be_turned_off(client):
    sid, _, _ = _game(client)
    assert rs.load_session(sid)["config"]["chat"] is True
    sid2, _, _ = _game(client, {"chat": False})
    assert rs.load_session(sid2)["config"]["chat"] is False


def test_legal_and_illegal_offers(client):
    sid, a, b = _game(client)
    assert _act(client, sid, b, "offer", {"keep": {"silk": 1}}).status_code == 400, "creator moves first"
    assert _act(client, sid, a, "offer", {"keep": {"silk": 9}}).status_code == 400
    assert _act(client, sid, a, "offer", {"keep": {"gold": 1}}).status_code == 400
    assert _act(client, sid, a, "accept").status_code == 400, "nothing on the table yet"
    assert _act(client, sid, a, "offer", {"keep": {"silk": 1}}).status_code == 200


def test_the_hands_are_not_dealt_before_the_round_is_published(client):
    a, b = f"gg_{uuid.uuid4().hex[:6]}", f"gh_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, a)
    _register_keyed(client, b)
    from conftest import signed_session_body
    from emporia import wire
    body = signed_session_body(a, {"module_type": BARGAIN})
    sid = wire.session_id_for(body["nonce"])
    commit = make_test_commit(sid, a, 0, round_offset=1000)         # ~50 minutes ahead
    assert client.post("/sessions", json={**body, "rand_commit": commit}, headers=_hdr(a)).status_code == 200
    assert client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers=_hdr(b)).status_code == 200
    r = _act(client, sid, a, "offer", {"keep": {"silk": 1}})
    assert r.status_code == 400 and "not dealt yet" in r.text and str(commit["beacon_round"]) in r.text


def test_the_game_uses_the_latest_round_named(client):
    sid, a, b = _game(client)
    rounds = [c["beacon_round"] for c in rs.load_session(sid)["config"]["rand"]["commits"].values()]
    assert rs.load_session(sid)["state"]["data"]["rand"]["round"] == max(rounds)
    with rs.DB_LOCK, rs.get_db() as conn:     # the signed commitments are rows, and so in the transparency log
        got = conn.execute("SELECT agent_id, seat FROM rand_commits WHERE session_id = ? ORDER BY seat", (sid,)).fetchall()
    assert [(r["agent_id"], r["seat"]) for r in got] == [(a, 0), (b, 1)]


def test_an_accepted_offer_is_scored_from_both_revealed_hands(client):
    sid, a, b = _game(client)
    _play_deal(client, sid, a, b, {"silk": 2, "amber": 1})
    assert rs.load_session(sid)["status"] == "active", "the reveal comes before the result"
    assert _act(client, sid, a, "offer", {"keep": {}}).status_code == 400
    ha, hb = _hand(sid, a), _hand(sid, b)
    assert _reveal(client, sid, a).status_code == 200
    assert rs.load_session(sid)["status"] == "active", "one reveal is not enough"
    assert _reveal(client, sid, a).status_code == 400, "no second reveal"
    rb = _reveal(client, sid, b)
    assert rb.status_code == 200, rb.text
    s, out = _outcome(sid)
    assert s["status"] == "completed"
    if ha[0] == hb[0]:
        assert out["termination"] == "collision"
        return
    pa = B.worth(ha[0], (2, 0, 1))
    pb = B.worth(hb[0], (1, 2, 0))
    assert out["termination"] == "deal" and out["points"] == {a: pa, b: pb}
    assert out["rating_scores"] == {a: pa / 10, b: pb / 10}
    assert out["values"] == {a: B._as_dict(ha[0]), b: B._as_dict(hb[0])}
    assert out["winner"] == (a if pa > pb else b if pb > pa else None)
    assert out["walk_aways"] == {a: ha[1], b: hb[1]}


def test_declining_the_last_offer_scores_each_its_own_walk_away(client):
    sid, a, b = _game(client, {"max_offers": 2})
    assert _act(client, sid, a, "offer", {"keep": {"silk": 3}}).status_code == 200
    assert _act(client, sid, b, "offer", {"keep": {"silk": 3}}).status_code == 200
    assert _act(client, sid, a, "decline").status_code == 200
    ha, hb = _hand(sid, a), _hand(sid, b)
    _reveal(client, sid, a)
    _reveal(client, sid, b)
    s, out = _outcome(sid)
    if ha[0] == hb[0]:
        assert out["termination"] == "collision"
        return
    assert out["termination"] == "no_deal" and out["points"] == {a: ha[1], b: hb[1]}


def test_a_reveal_must_be_the_committed_secret_and_only_after_the_end(client):
    sid, a, b = _game(client)
    s, nonce = RAND_SECRETS[(sid, a)]
    assert _act(client, sid, a, "reveal", {"s": s, "nonce": nonce}).status_code == 400, "not before it ends"
    _play_deal(client, sid, a, b)
    r = _act(client, sid, a, "reveal", {"s": "00" * 32, "nonce": nonce})
    assert r.status_code == 400 and "hashes" in r.text
    assert _act(client, sid, a, "reveal", {"s": s}).status_code == 400
    assert _act(client, sid, b, "reveal", {"s": s, "nonce": nonce}).status_code == 400, "another's secret is not yours"


def test_a_refused_reveal_forfeits_and_the_other_scores_by_its_own_hand(client):
    sid, a, b = _game(client)
    _play_deal(client, sid, a, b, {"silk": 3})
    assert _reveal(client, sid, b).status_code == 200
    _expire(sid, field="reveal_by", window=0)
    s = rs.load_session(sid)
    s["state"]["data"]["reveal_by"] = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    rs.save_session(session_id=sid, module_type=BARGAIN, config=s["config"], payment_rules=s["payment_rules"],
                    state=SessionState.from_dict(s["state"]), current_agent=s["current_agent"],
                    step_number=s["step_number"], status="active", participants=s["participants"],
                    created_at=s["created_at"], updated_at=datetime.now(UTC).isoformat())
    _sweep()
    s, out = _outcome(sid)
    hb = _hand(sid, b)
    assert s["status"] == "completed" and out["termination"] == "forfeit"
    assert out["points"] == {a: 0, b: B.worth(hb[0], (0, 2, 1))}
    assert out["rating_scores"][a] == 0.0 and set(out["values"]) == {b}, "only the revealed hand is published"


def test_both_refusing_is_no_result(client):
    sid, a, b = _game(client)
    _play_deal(client, sid, a, b)
    s = rs.load_session(sid)
    s["state"]["data"]["reveal_by"] = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    rs.save_session(session_id=sid, module_type=BARGAIN, config=s["config"], payment_rules=s["payment_rules"],
                    state=SessionState.from_dict(s["state"]), current_agent=s["current_agent"],
                    step_number=s["step_number"], status="active", participants=s["participants"],
                    created_at=s["created_at"], updated_at=datetime.now(UTC).isoformat())
    _sweep()
    s, out = _outcome(sid)
    assert s["status"] == "completed" and out["termination"] == "unrevealed" and out["winner"] is None
    assert progression._scores({"participants": [a, b], **out}) is None, "counts for nothing"


def test_two_players_dealt_the_same_hand_have_no_game(client, monkeypatch):
    monkeypatch.setattr(B, "HANDS", ((2, 1, 2),))
    sid, a, b = _game(client)
    _play_deal(client, sid, a, b)
    _reveal(client, sid, a)
    _reveal(client, sid, b)
    s, out = _outcome(sid)
    assert s["status"] == "completed" and out["termination"] == "collision" and out["winner"] is None
    assert progression._scores({"participants": [a, b], **out}) is None, "a collision counts nothing"


def _future_game(client, ahead=1000):
    """A game whose round is `ahead` rounds away: the hands cannot be seen yet."""
    from conftest import signed_session_body

    from emporia import wire
    a, b = f"gz_{uuid.uuid4().hex[:6]}", f"gy_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, a)
    _register_keyed(client, b)
    body = signed_session_body(a, {"module_type": BARGAIN})
    sid = wire.session_id_for(body["nonce"])
    commit = make_test_commit(sid, a, 0, round_offset=ahead)
    assert client.post("/sessions", json={**body, "rand_commit": commit}, headers=_hdr(a)).status_code == 200
    assert client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers=_hdr(b)).status_code == 200
    return sid, a, b


def test_a_late_commit_void_before_the_round_is_accepted_and_is_no_result(client):
    sid, a, b = _future_game(client)
    assert _act(client, sid, b, "void", {"reason": "other"}).status_code == 400
    assert _act(client, sid, b, "void", {"reason": "late_commit"}).status_code == 200
    s, out = _outcome(sid)
    assert s["status"] == "completed" and out["termination"] == "void" and out["voided_by"] == b
    assert progression._scores({"participants": [a, b], **out}) is None, "no result, no penalty"


def test_a_void_after_the_round_is_published_is_refused(client):
    sid, a, b = _game(client)          # the shim names a round already published
    r = _act(client, sid, b, "void", {"reason": "late_commit"})
    assert r.status_code == 400 and "Too late to void" in r.text
    assert rs.load_session(sid)["status"] == "active"


def test_seeing_the_hand_and_then_voiding_to_reroll_fails(client):
    sid, a, b = _game(client)
    hand, _walk = _hand(sid, b)       # the round is out: b can see its hand and dislikes it
    assert hand in B.HANDS
    assert _act(client, sid, b, "void", {"reason": "late_commit"}).status_code == 400
    assert _act(client, sid, a, "offer", {"keep": {"silk": 1}}).status_code == 200, "the game goes on"


def test_the_void_is_on_the_voiders_record_and_not_the_others(client):
    sid, a, b = _future_game(client)
    _act(client, sid, b, "void", {"reason": "late_commit"})
    rec_b = client.get(f"/agents/{b}/record", headers=_hdr(a)).json()["games"]
    rec_a = client.get(f"/agents/{a}/record", headers=_hdr(b)).json()["games"]
    assert rec_b["voided"] == 1 and rec_a["voided"] == 0
    assert rec_b["finished"] == 0 and rec_a["finished"] == 0, "a void is no finished game for either"


def test_the_client_voids_at_round_time_minus_a_margin_not_after():
    from emporia import rand_client
    sess_id, now = "sess_v", rand.GENESIS + 1_000_000 * rand.PERIOD_S
    (ca, sa), (cb, _sb) = (rand_client.make_commit(lambda *x: "s", sess_id, "a", 0, now=now),
                           rand_client.make_commit(lambda *x: "s", sess_id, "b", 1, now=now + 3))
    sess = {"session_id": sess_id, "config": {"rand": {"scope": sess_id, "commits": {
        "a": {**ca, "seat": 0}, "b": {**cb, "seat": 1}}}}, "state": {"data": {"pool": B._as_dict(POOL), "offers": []}}}
    rnd = B.game_round(sess["config"]["rand"]["commits"])
    t = rand.round_time(rnd)
    seen_late = dict(sa, seen_all_at=t - 2)          # inside the margin: too late to trust
    assert rand_client.private_view(sess, "a", seen_late, now=t - 1)["void"] == "late_commit"
    seen_ok = dict(sa, seen_all_at=t - 30)
    assert "void" not in rand_client.private_view(sess, "a", seen_ok, now=t - 1)
    after = rand_client.private_view(sess, "a", dict(seen_late), now=t + 1, fetch=lambda r, base=None: fake_beacon_sig(r))
    assert "void" not in after and "values" in after, "after the round a void is refused, so none is offered"


def test_running_out_of_time_loses_and_scores_zero(client):
    sid, a, b = _game(client)
    _act(client, sid, a, "offer", {"keep": {"silk": 1}})
    _expire(sid)
    _sweep()
    s, out = _outcome(sid)
    assert s["status"] == "completed" and out["winner"] == a and out["termination"] == "timeout"
    assert out["points"] == {b: 0, a: 0}


def test_an_opener_who_never_opens_loses_on_time_review2_p2(client):
    """Review pass 2, P2: both are committed when the game starts, so letting the first clock run out is
    no free reroll: the opener loses on time."""
    sid, a, b = _game(client, {"move_time_limit_s": 60})
    _expire(sid)
    _sweep()
    s, out = _outcome(sid)
    assert s["status"] == "completed" and out["winner"] == b and out["termination"] == "timeout"
    assert progression._scores({"participants": [a, b], **out}) == {(b, a): 1.0, (a, b): 0.0}
    assert s["state"]["data"]["leaving_counts"] is True, "an abandon at step 0 is a walk-away"


def test_no_stakes_and_no_bounty_on_a_game_that_draws_from_the_beacon(client):
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


# ── verify_session ───────────────────────────────────────────────────────────

def _record(client, sid, a, b):
    return {
        "session": client.get(f"/sessions/{sid}", headers=_hdr(a)).json(),
        "log": client.get(f"/sessions/{sid}/audit", headers=_hdr(a)).json()["chain"],
        "public_keys": {x: client.get(f"/agents/{x}", headers=_hdr(a)).json()["public_key_hex"] for x in (a, b)},
        "receipts": [], "relay_key": client.get("/health").json()["relay_public_key"],
        "relay_verdict": next(r for r in client.get("/results", params={"agent_id": a}).json()["results"]
                              if r["session_id"] == sid),
    }


def test_verify_session_rechecks_a_finished_game_and_catches_forgery(client, monkeypatch):
    from emporia import beacon
    from emporia.verify import verify_game
    monkeypatch.setattr(beacon, "verify", lambda rnd, sig: sig == fake_beacon_sig(rnd))   # the BLS check: test_rand.py
    sid, a, b = _game(client)
    _play_deal(client, sid, a, b)
    _reveal(client, sid, a)
    _reveal(client, sid, b)
    record = _record(client, sid, a, b)
    record["relay_verdict"] = {**record["relay_verdict"], **_outcome(sid)[1]}     # with the published hands
    ok = next(f for f in verify_game(**record)["findings"] if f["check"] == "rand")
    assert ok["status"] == "ok", ok

    def broken(mutate):
        forged = copy.deepcopy(record)
        mutate(forged["session"])
        return next(f for f in verify_game(**forged)["findings"] if f["check"] == "rand")["status"]
    assert broken(lambda s: s["state"]["data"]["rand"]["reveals"][b].update(s="11" * 32)) == "fail", "a secret off its hash"
    assert broken(lambda s: s["state"]["data"]["rand"].update(beacon_sig="22" * 48)) == "fail", "not drand's signature"
    assert broken(lambda s: s["config"]["rand"]["commits"][a].update(signature="x" * 86)) == "fail"
    assert broken(lambda s: s["config"]["rand"]["commits"][a].update(beacon_round=1)) == "fail", "a round already known"
    swapped = copy.deepcopy(record)
    swapped["relay_verdict"]["values"] = {a: {"silk": 9, "spice": 0, "amber": 0}, b: swapped["relay_verdict"]["values"][b]}
    assert next(f for f in verify_game(**swapped)["findings"] if f["check"] == "rand")["status"] == "fail"


# ── the module on its own ────────────────────────────────────────────────────

def test_the_players_view_is_computed_client_side_from_its_secret(monkeypatch):
    from emporia import rand_client
    sess_id = "sess_view"
    now = rand.GENESIS + 1_000_000 * rand.PERIOD_S
    rec_a = rand_client.make_commit(lambda *a: "sig", sess_id, "a", 0, now=now)
    rec_b = rand_client.make_commit(lambda *a: "sig", sess_id, "b", 1, now=now + 5)
    (ca, sa), (cb, _sb) = rec_a, rec_b
    assert ca["beacon_round"] == rand.round_at(now) + rand.CLIENT_LEAD_ROUNDS
    assert not rand.commit_round_problem(ca["at"], ca["beacon_round"]), "the tools always name a legal round"
    sess = {"session_id": sess_id, "config": {"rand": {"scope": sess_id, "commits": {"a": {**ca, "seat": 0}, "b": {**cb, "seat": 1}}}},
            "state": {"data": {"pool": B._as_dict(POOL), "offers": [
                {"n": 1, "by": "a", "allocation": {"a": B._as_dict((3, 0, 0)), "b": B._as_dict((0, 2, 1))}}]}}}
    rnd = B.game_round(sess["config"]["rand"]["commits"])
    early = rand_client.private_view(sess, "a", dict(sa), now=now + 1)
    assert early["wait_s"] > 0 and "values" not in early, "no hand before the round exists"
    ready = rand.round_time(rnd) + 1
    view = rand_client.private_view(sess, "a", dict(sa, seen_all_at=now + 1), now=ready, fetch=lambda r, base=None: fake_beacon_sig(r))
    hand, walk = B.draw(sess_id, 0, bytes.fromhex(fake_beacon_sig(rnd)), bytes.fromhex(sa["s"]))
    assert view["values"] == B._as_dict(hand) and view["walk_away"] == walk and view["all_goods_worth"] == 10
    assert view["offers"] == [{"n": 1, "by": "a", "worth_to_you": B.worth(hand, (3, 0, 0))}]
    # a client that first saw the other commitment only after the round voids
    late = dict(sa, seen_all_at=rand.round_time(rnd) - 1)
    assert rand_client.private_view(sess, "a", late, now=rand.round_time(rnd) - 0.5)["void"] == "late_commit"


def test_the_mcp_tools_commit_on_create_and_join_and_reveal_from_the_local_secret(monkeypatch, tmp_path):
    from emporia import identity, mcp_server as m, wire

    class R:
        def __init__(self, body, code=200):
            self._b, self.status_code = body, code

        def json(self):
            return self._b

        def raise_for_status(self):
            pass

    sess = {"session_id": "sess_t", "module_type": BARGAIN, "step_number": 4, "status": "active",
            "participants": ["x", "me"], "config": {}, "payment_rules": {},
            "state": {"data": {"pool": B._as_dict(POOL), "offers": [], "max_offers": 6, "ended": {"how": "accepted"}}}}
    posted = []

    def fake(method, url, **kw):
        if method == "post":
            posted.append((url, kw.get("json")))
            return R({"session_id": "sess_t"})
        return R(sess)
    monkeypatch.setattr(m, "_bearer_request", fake)
    me = f"gm_{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(m, "AGENT_ID", me)
    identity.get_public_key_hex(me)
    r = m.join_session("sess_t", agent_id=me)
    commit = posted[-1][1]["rand_commit"]
    assert identity.verify(wire.signed_rand_commit_payload("sess_t", stable_of(me), 2, commit["hash"], commit["beacon_round"],
                                                           commit["at"]), commit["signature"],
                           identity.get_public_key_hex(me)), "seat = players already seated"
    assert not rand.commit_round_problem(commit["at"], commit["beacon_round"]) and "s" not in commit
    m.submit_action("sess_t", "reveal", {}, WHY, agent_id=me)
    sent = posted[-1][1]["payload"]
    assert rand.commit_hash(bytes.fromhex(sent["s"]), bytes.fromhex(sent["nonce"])) == commit["hash"]
    created = []
    monkeypatch.setattr(m, "_offer_session", lambda *a: False)
    m.create_session("emporia:bargain:v2", agent_id=me)
    created.append(posted[-1][1])
    c = created[0]["rand_commit"]
    assert identity.verify(wire.signed_rand_commit_payload(wire.session_id_for(created[0]["nonce"]), stable_of(me), 0, c["hash"],
                                                           c["beacon_round"], c["at"]), c["signature"],
                           identity.get_public_key_hex(me))
    assert r.get("success") is True


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
    monkeypatch.setattr(B, "draw", lambda scope, seat, sig, s: (B.HANDS[2 + 3 * seat], seat))   # two different hands
    _act(client, sid, a, "offer", {"keep": {"silk": 1}})
    _act(client, sid, h, "accept")
    _reveal(client, sid, a)
    _reveal(client, sid, h)
    standing._TABLE_CACHE.update(key=None, value=None)
    assert sid not in {g["session_id"] for g in standing._craft_games()}, "practice with the house counts nothing"
    games = standing._public_games_of(a)
    assert any(g["session_id"] == sid and g["termination"] == "deal" for g in games)
    done = {q["id"] for q in quests.progress(a, posts=[], games=games, deals=[])["quests"] if q["done"]}
    assert {"first-set-play", "bargain"} <= done, "XP only: the house counts for quests"


def test_strike_a_bargain_needs_a_deal_and_is_never_next():
    from emporia import quests
    g = {"session_id": "s", "module_type": BARGAIN, "participants": ["me", "x"], "ended_at": "2026-11-05"}
    none = quests.progress("me", posts=[], games=[{**g, "termination": "no_deal"}], deals=[])
    assert not next(q for q in none["quests"] if q["id"] == "bargain")["done"]
    assert "bargain" not in none["next"]
    deal = quests.progress("me", posts=[], games=[{**g, "termination": "deal"}], deals=[])
    q = next(q for q in deal["quests"] if q["id"] == "bargain")
    assert q["done"] and q["optional"] and deal["xp"] == 10 + 10


def test_bargain_counts_for_craft_and_is_not_engine_decided():
    from emporia.routes.standing import craft_modules
    assert BARGAIN in craft_modules()
    assert get_interaction_module(BARGAIN).ENGINE_DECIDABLE is False


# ── the house's honest policy ────────────────────────────────────────────────
