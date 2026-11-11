"""The games pack: Liar's dice (`emporia:liars-dice:v1`) and the common-value auction (`emporia:cv-auction:v1`).

Both draw from `emporia:rand:v1`: a player's dice or signal come from its own committed secret and a public
drand round, so the relay holds none while the game runs. These tests use the shim's fake beacon (conftest
`rand_testmode`, recorded rounds, never the network); the real BLS check has its own tests in test_rand.py."""
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import RAND_SECRETS, fake_beacon_sig, make_test_commit, signed_session_body
from fastapi.testclient import TestClient

from emporia import commit_reveal, progression, rand, wire
from emporia import relay_server as rs
from emporia.module_sdk import SessionState, get_interaction_module
from emporia.modules import cv_auction as CV
from emporia.modules import liars_dice as LD
from emporia.relay_server import app
from tests.test_emporia import _register_keyed, _signed_action_body

DICE, AUCTION = "emporia:liars-dice:v1", "emporia:cv-auction:v1"
WHY = "Reading the table and what the bids imply"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51232))


def _hdr(agent):
    return {"x-emporia-agent-id": agent}


def _game(client, module, n, config=None, ahead=None):
    agents = [f"gp{i}_{uuid.uuid4().hex[:6]}" for i in range(n)]
    for a in agents:
        _register_keyed(client, a)
    cfg = {"players": n, **(config or {})}
    if ahead is None:
        r = client.post("/sessions", json={"module_type": module, "config": cfg}, headers=_hdr(agents[0]))
    else:
        body = signed_session_body(agents[0], {"module_type": module, "config": cfg})
        sid0 = wire.session_id_for(body["nonce"])
        r = client.post("/sessions", json={**body, "rand_commit": make_test_commit(sid0, agents[0], 0, round_offset=ahead)},
                        headers=_hdr(agents[0]))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    for i, b in enumerate(agents[1:], start=1):
        if ahead is None:
            r = client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers=_hdr(b))
        else:
            body = {"agent_id": b, "rand_commit": make_test_commit(sid, b, i, round_offset=ahead)}
            r = client.post(f"/sessions/{sid}/join", json=body, headers=_hdr(b))
        assert r.status_code == 200, r.text
    return sid, agents


def _act(client, sid, who, action, payload=None, say=""):
    return client.post(f"/sessions/{sid}/action",
                       json=_signed_action_body(client, sid, who, action, payload or {}, WHY, say=say))


def _reveal(client, sid, who):
    s, nonce = RAND_SECRETS[(sid, who)]
    return _act(client, sid, who, "reveal", {"s": s, "nonce": nonce})


def _save(sid, s, state):
    rs.save_session(session_id=sid, module_type=s["module_type"], config=s["config"],
                    payment_rules=s["payment_rules"], state=state, current_agent=s["current_agent"],
                    step_number=s["step_number"], status="active", participants=s["participants"],
                    created_at=s["created_at"], updated_at=datetime.now(UTC).isoformat())


def _age(sid, field, seconds):
    s = rs.load_session(sid)
    s["state"]["data"][field] = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    _save(sid, s, SessionState.from_dict(s["state"]))


def _sweep():
    from emporia.routes.sessions import sweep_service_deadlines
    asyncio.run(sweep_service_deadlines())


def _outcome(sid):
    s = rs.load_session(sid)
    return s, get_interaction_module(s["module_type"]).is_terminal(SessionState.from_dict(s["state"]))[1]


def _dice_of(sid, who, sess=None):
    s = sess or rs.load_session(sid)
    r = s["state"]["data"]["rand"]
    return LD.draw(r["scope"], s["participants"].index(who), bytes.fromhex(fake_beacon_sig(r["round"])),
                   bytes.fromhex(RAND_SECRETS[(sid, who)][0]), s["state"]["data"]["dice"])


def _order(sid):
    s = rs.load_session(sid)
    r = s["state"]["data"]["rand"]
    return LD.turn_order(r["scope"], bytes.fromhex(fake_beacon_sig(r["round"])), s["participants"])


def _first_bid(client, sid):
    """Start the dice game: the first in the beacon's order makes a bid (the state gets the round then)."""
    first = _order(sid)[0]
    assert _act(client, sid, first, "bid", {"quantity": 1, "face": 2}, say="a modest start").status_code == 200
    return first


# ── fixtures: the draws are pure functions of the recorded round and the secret ──────────────────

def test_the_draws_are_deterministic_from_a_recorded_round_and_secret():
    sig, s = bytes.fromhex(fake_beacon_sig(12345)), bytes(range(32))
    assert LD.draw("sess_x", 1, sig, s) == LD.draw("sess_x", 1, sig, s) == [1, 6, 2, 5, 2]
    assert LD.draw("sess_x", 1, sig, s, 3) == [1, 6, 2], "fewer dice are the first of the same five"
    assert sorted(LD.turn_order("sess_x", sig, ["a", "b", "c"])) == ["a", "b", "c"]
    assert CV.base_value("sess_x", sig) == 88
    assert CV.part("sess_x", 1, sig, s, 10) == -2
    assert LD.draw("sess_x", 2, sig, s) != LD.draw("sess_x", 1, sig, s), "a seat's draw is its own"
    seen = {d for i in range(300) for d in LD.draw("sess_x", 0, sig, i.to_bytes(32, "big"))}
    assert seen == set(range(1, 7))
    parts = {CV.part("sess_x", 0, sig, i.to_bytes(32, "big"), 10) for i in range(500)}
    assert parts == set(range(-10, 11))
    with pytest.raises(ValueError):
        rand.priv("sess_x", 0, "die:0", b"", s)


def test_a_bid_must_be_higher_and_the_cap_forces_a_call():
    m = LD.LiarsDiceModule()
    data = {"players": 3, "dice": 5, "max_bids": 3, "bids": [{"quantity": 3, "face": 4}]}
    assert m.move_problem(data, {"quantity": 3, "face": 5}) == ""
    assert m.move_problem(data, {"quantity": 4, "face": 1}) == ""
    for bad in ({"quantity": 3, "face": 4}, {"quantity": 2, "face": 6}, {"quantity": 16, "face": 1},
                {"quantity": 3, "face": 7}, {"quantity": True, "face": 1}, {"quantity": 3}):
        assert m.move_problem(data, bad), bad
    assert "cap" in m.move_problem({**data, "bids": [{"quantity": 1, "face": 1}] * 3}, {"quantity": 9, "face": 6})


# ── Liar's dice ───────────────────────────────────────────────────────────────

def test_the_relay_stores_no_die_and_no_secret_while_the_game_runs(client):
    sid, agents = _game(client, DICE, 3)
    first = _first_bid(client, sid)
    s = rs.load_session(sid)
    blob = json.dumps(s)
    for who in agents:
        sec, nonce = RAND_SECRETS[(sid, who)]
        assert sec not in blob and nonce not in blob, "no secret is held before the reveal"
    assert s["state"]["data"]["rand"]["reveals"] == {}
    d = s["state"]["data"]
    assert not {"hands", "values", "signals", "my_dice"} & set(d) and isinstance(d["dice"], int), d.keys()
    assert d["bids"][0]["by"] == first and d["rand"]["beacon_sig"] == fake_beacon_sig(d["rand"]["round"])
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT * FROM rand_commits WHERE session_id = ?", (sid,)).fetchall()
        assert len(rows) == 3 and not any(RAND_SECRETS[(sid, r["agent_id"])][0] in json.dumps(dict(r)) for r in rows)
    view = client.get(f"/sessions/{sid}", headers=_hdr(agents[0])).json()
    assert view["state"]["data"]["turn_order"] == _order(sid), "the order is public, the dice are not"


def test_turns_follow_the_beacon_order_and_a_bid_must_beat_the_last(client):
    sid, agents = _game(client, DICE, 3)
    order = _order(sid)
    assert _act(client, sid, order[1], "bid", {"quantity": 1, "face": 2}).status_code == 400, "not the first player"
    assert _act(client, sid, order[0], "liar").status_code == 400, "nothing to call yet"
    assert _act(client, sid, order[0], "bid", {"quantity": 2, "face": 3}).status_code == 200
    assert _act(client, sid, order[0], "bid", {"quantity": 3, "face": 3}).status_code == 400, "not your turn"
    r = _act(client, sid, order[1], "bid", {"quantity": 2, "face": 3})
    assert r.status_code == 400 and "higher" in r.text
    assert _act(client, sid, order[1], "bid", {"quantity": 2, "face": 4}, say="four fours? ok").status_code == 200
    chat = client.get(f"/sessions/{sid}/chat", headers=_hdr(agents[0])).json()
    assert [m["content"] for m in chat["messages"]] == ["four fours? ok"] and "untrusted" in chat


@pytest.mark.parametrize("bid_true", [True, False])
def test_a_call_ends_the_play_and_the_referee_decides_from_the_revealed_dice(client, bid_true):
    sid, agents = _game(client, DICE, 3, {"dice": 2})
    order = _order(sid)
    allv = {w: _dice_of(sid, w) for w in agents}
    def seen(f):
        return sum(d == f for ds in allv.values() for d in ds)
    face = max(range(1, 7), key=seen) if bid_true else min(range(1, 7), key=seen)
    true_count = seen(face)
    q = true_count if bid_true else true_count + 1     # a bid of exactly what is there, or one more than is
    assert _act(client, sid, order[0], "bid", {"quantity": q, "face": face}).status_code == 200
    assert _act(client, sid, order[1], "liar", say="I do not believe you").status_code == 200
    assert rs.load_session(sid)["status"] == "active", "the play is over; the reveals are awaited"
    assert _act(client, sid, order[2], "bid", {"quantity": 6, "face": 6}).status_code == 400
    for w in agents:
        assert _reveal(client, sid, w).status_code == 200
    s, out = _outcome(sid)
    assert s["status"] == "completed" and out["count"] == true_count
    held = true_count >= q
    assert held == bid_true
    winner, loser = (order[0], order[1]) if held else (order[1], order[0])
    assert out["winner"] == winner and out["points"][winner] == 1 and out["points"][loser] == -1
    assert out["points"][order[2]] == 0 and sum(out["points"].values()) == 0, "zero-sum"
    assert out["values"] == allv and out["dice_after"][loser] == 1 and out["dice_after"][winner] == 2
    assert out["rating_scores"][order[2]] == 0.5
    from emporia.verify import _rand_check
    session = rs.load_session(sid)
    assert _rand_check(get_interaction_module(DICE), session, {}, out) is not None


def test_ones_wild_counts_ones_as_every_face():
    assert LD.count_face({"a": [1, 1, 3], "b": [3, 2]}, 3, False) == 2
    assert LD.count_face({"a": [1, 1, 3], "b": [3, 2]}, 3, True) == 4
    assert LD.count_face({"a": [1, 1, 3]}, 1, True) == 2, "a ones bid counts a one once"


def test_a_refused_reveal_forfeits_each_refuser_loses_and_a_revealer_takes_the_sum(client):
    sid, agents = _game(client, DICE, 3)
    order = _order(sid)
    assert _act(client, sid, order[0], "bid", {"quantity": 2, "face": 5}).status_code == 200
    assert _act(client, sid, order[1], "liar").status_code == 200
    assert _reveal(client, sid, order[0]).status_code == 200
    _age(sid, "reveal_by", 5)
    _sweep()
    s, out = _outcome(sid)
    assert s["status"] == "completed" and out["termination"] == "forfeit"
    assert out["points"] == {order[0]: 0, order[1]: -1, order[2]: -1} or out["points"][order[0]] == 2
    assert out["winner"] == order[0] and out["points"][order[0]] == 2 and sum(out["points"].values()) == 0
    assert set(out["values"]) == {order[0]}, "only the revealed dice are published"
    assert out["rating_scores"][order[1]] == 0.0 and out["rating_scores"][order[0]] == 0.5


def test_nobody_revealing_is_no_result(client):
    sid, agents = _game(client, DICE, 2)
    order = _order(sid)
    _act(client, sid, order[0], "bid", {"quantity": 1, "face": 1})
    _act(client, sid, order[1], "liar")
    _age(sid, "reveal_by", 5)
    _sweep()
    s, out = _outcome(sid)
    assert s["status"] == "completed" and out["termination"] == "unrevealed" and out["winner"] is None
    assert progression._scores({"participants": agents, **out}) is None


def test_a_player_who_runs_out_of_time_loses_and_the_one_before_it_scores(client):
    sid, agents = _game(client, DICE, 3)
    order = _order(sid)
    _act(client, sid, order[0], "bid", {"quantity": 1, "face": 5})
    _age(sid, "turn_started_at", 601)
    _sweep()
    s, out = _outcome(sid)
    assert s["status"] == "completed" and out["termination"] == "timeout"
    assert out["winner"] == order[0] and out["points"][order[1]] == -1 and out["points"][order[0]] == 1
    # the beacon the result was read with stays with the game
    assert s["state"]["data"]["rand"]["beacon_sig"]


def test_a_late_commit_void_before_the_round_is_no_result_and_after_it_is_refused(client):
    sid, agents = _game(client, DICE, 3, ahead=1000)
    assert _act(client, sid, agents[1], "void", {"reason": "other"}).status_code == 400
    assert _act(client, sid, agents[1], "bid", {"quantity": 1, "face": 1}).status_code == 400, "nothing is dealt yet"
    assert _act(client, sid, agents[1], "void", {"reason": "late_commit"}).status_code == 200
    s, out = _outcome(sid)
    assert s["status"] == "completed" and out["termination"] == "void" and out["voided_by"] == agents[1]
    assert progression._scores({"participants": agents, **out}) is None
    sid2, agents2 = _game(client, DICE, 3)          # the round is out: seeing dice and voiding would be a reroll
    r = _act(client, sid2, agents2[0], "void", {"reason": "late_commit"})
    assert r.status_code == 400 and "Too late to void" in r.text


def test_a_reveal_must_match_the_commitment_and_waits_for_the_call(client):
    sid, agents = _game(client, DICE, 2)
    assert _reveal(client, sid, agents[0]).status_code == 400, "not before a call"
    order = _order(sid)
    _act(client, sid, order[0], "bid", {"quantity": 1, "face": 1})
    _act(client, sid, order[1], "liar")
    bad = _act(client, sid, agents[0], "reveal", {"s": "ab" * 32, "nonce": "cd" * 32})
    assert bad.status_code == 400 and "commitment" in bad.text
    assert _reveal(client, sid, agents[0]).status_code == 200
    assert _reveal(client, sid, agents[0]).status_code == 400, "once"


def test_two_players_make_a_game_and_the_set_play_counts():
    from emporia import playbook, quests
    assert DICE in quests.SET_PLAYS and AUCTION in quests.SET_PLAYS
    assert DICE in playbook.SET_PLAYS and AUCTION in playbook.SET_PLAYS
    for mt, lo, hi in ((DICE, 2, 6), (AUCTION, 3, 8)):
        cls = get_interaction_module(mt).__class__
        assert (cls.MIN_PARTICIPANTS, cls.MAX_PARTICIPANTS) == (lo, hi)
        assert cls.ZERO_SUM and cls.USES_RAND and cls.PRICE_MODEL is None and cls.CHAT_BY_DEFAULT
    assert DICE not in progression.non_zero_sum() and AUCTION not in progression.non_zero_sum()


def test_games_of_chance_take_no_stake(client):
    a = f"gs_{uuid.uuid4().hex[:6]}"
    _register_keyed(client, a)
    for mt in (DICE, AUCTION):
        r = client.post("/sessions", json={"module_type": mt, "payment_rules": {"mode": "x402",
                                                                               "price": "5"}},
                        headers=_hdr(a))
        assert r.status_code == 400 and "takes no stake" in r.text


# ── the common-value auction ──────────────────────────────────────────────────

def _bid(client, sid, who, amount):
    nonce = commit_reveal.new_nonce()
    r = _act(client, sid, who, "commit", {"commitment": commit_reveal.commitment(amount, nonce)})
    assert r.status_code == 200, r.text
    return nonce


def _auction(client, bids, n=3, spread=10):
    sid, agents = _game(client, AUCTION, n, {"spread": spread})
    nonces = {w: _bid(client, sid, w, amt) for w, amt in zip(agents, bids)}
    for w, amt in zip(agents, bids):
        r = _act(client, sid, w, "reveal", {"move": amt, "nonce": nonces[w]})
        assert r.status_code == 200, r.text
    return sid, agents


def _truth(sid):
    s = rs.load_session(sid)
    d = s["state"]["data"]
    sig = bytes.fromhex(fake_beacon_sig(d["rand"]["round"]))
    parts = {w: CV.part(d["rand"]["scope"], i, sig, bytes.fromhex(RAND_SECRETS[(sid, w)][0]), d["spread"])
             for i, w in enumerate(s["participants"])}
    base = CV.base_value(d["rand"]["scope"], sig)
    return base, parts, base + sum(parts.values())


def test_the_relay_stores_no_signal_and_the_bids_are_sealed_until_everyone_is_in(client):
    sid, agents = _game(client, AUCTION, 3)
    n0 = _bid(client, sid, agents[0], 77)
    d = rs.load_session(sid)["state"]["data"]
    assert d["commitments"][agents[0]] == commit_reveal.commitment(77, n0) and d["revealed"] == {}
    assert not {"signals", "values", "parts", "value"} & set(d)
    assert _act(client, sid, agents[0], "reveal", {"move": 77, "nonce": n0}).status_code == 400, "not the reveal phase"
    assert _act(client, sid, agents[0], "commit", {"commitment": commit_reveal.commitment(1, n0)}).status_code == 400
    assert _act(client, sid, agents[1], "reveal", {"s": "00" * 32, "nonce": "00" * 32}).status_code == 400


def test_the_highest_bid_wins_value_minus_bid_and_the_others_score_zero(client):
    sid, agents = _auction(client, [10, 500 - 300, 5])      # a modest, a high, a low bid (200 is within 0-300)
    for w in agents:
        assert _reveal(client, sid, w).status_code == 200
    s, out = _outcome(sid)
    base, parts, value = _truth(sid)
    assert s["status"] == "completed" and out["value"] == value and out["base_value"] == base
    assert out["bidder"] == agents[1] and out["winning_bid"] == 200 and out["profit"] == value - 200
    assert out["points"] == {agents[0]: 0, agents[1]: value - 200, agents[2]: 0}
    assert out["values"] == {w: base + parts[w] for w in agents}, "the signals are published at the end"
    if value - 200 > 0:
        assert out["winner"] == agents[1] and out["termination"] == "auction"
    else:
        assert out["winner"] is None and out["termination"] in ("winners_curse", "break_even")
    from emporia.verify import _rand_check
    assert _rand_check(get_interaction_module(AUCTION), rs.load_session(sid), {}, out) is not None


def test_a_negative_result_is_the_winners_curse_and_the_rest_share_the_win():
    m = CV.CommonValueAuctionModule()
    data = {"rand": {"scope": "s", "reveals": {}, "beacon_sig": fake_beacon_sig(9)}, "spread": 10, "revealed": {"a": 300, "b": 1, "c": 2},
            "reveal_by": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    sig = bytes.fromhex(fake_beacon_sig(9))
    secrets = {p: bytes([i + 1]) * 32 for i, p in enumerate("abc")}
    data["rand"]["reveals"] = {p: {"s": s.hex(), "nonce": "00" * 32} for p, s in secrets.items()}
    over, out = m._settle(data, list("abc"))
    base = CV.base_value("s", sig)
    value = base + sum(CV.part("s", i, sig, secrets[p], 10) for i, p in enumerate("abc"))
    assert over and out["value"] == value and out["profit"] == value - 300 < 0
    assert out["termination"] == "winners_curse" and out["tied"] == ["b", "c"] and out["points"]["a"] == value - 300


def test_a_tie_for_the_highest_bid_is_broken_by_the_beacon_order(client):
    sid, agents = _auction(client, [50, 50, 20])
    for w in agents:
        _reveal(client, sid, w)
    s, out = _outcome(sid)
    d = s["state"]["data"]
    sig = bytes.fromhex(fake_beacon_sig(d["rand"]["round"]))
    expected = min(agents[:2], key=lambda p: rand.rand(d["rand"]["scope"], f"tiebreak:{agents.index(p)}", sig))
    assert out["bidder"] == expected


def test_a_refused_secret_reveal_forfeits_and_the_top_bidder_cannot_hide_a_loss(client):
    sid, agents = _auction(client, [10, 200, 5])
    assert _reveal(client, sid, agents[0]).status_code == 200
    assert _reveal(client, sid, agents[2]).status_code == 200
    _age(sid, "reveal_by", 5)
    _sweep()
    s, out = _outcome(sid)
    assert s["status"] == "completed" and out["termination"] == "forfeit" and out["winner"] is None
    assert out["points"] == {agents[0]: 0, agents[1]: -200, agents[2]: 0}, "the winning bidder who hides loses its bid"
    assert out["rating_scores"][agents[1]] == 0.0 and set(out["values"]) == {agents[0], agents[2]}


def test_a_bid_committed_and_not_revealed_in_time_is_out_and_no_commit_is_no_game(client):
    sid, agents = _game(client, AUCTION, 3)
    nonces = {w: _bid(client, sid, w, 40) for w in agents}
    _act(client, sid, agents[0], "reveal", {"move": 40, "nonce": nonces[agents[0]]})
    _age(sid, "phase_started_at", 601)
    _sweep()
    s, out = _outcome(sid)
    assert out["termination"] == "forfeit" and set(out["out"]) == set(agents[1:])
    sid, agents = _game(client, AUCTION, 3)
    _bid(client, sid, agents[0], 40)
    _age(sid, "phase_started_at", 601)
    _sweep()
    s, out = _outcome(sid)
    assert s["status"] == "completed" and out["termination"] == "timeout" and out["winner"] is None


def test_an_illegal_bid_is_refused_at_the_reveal_and_a_wrong_nonce_too(client):
    sid, agents = _game(client, AUCTION, 3)
    nonces = {w: _bid(client, sid, w, 301 if i == 0 else 30) for i, w in enumerate(agents)}
    r = _act(client, sid, agents[0], "reveal", {"move": 301, "nonce": nonces[agents[0]]})
    assert r.status_code == 400 and "not legal" in r.text
    r = _act(client, sid, agents[1], "reveal", {"move": 31, "nonce": nonces[agents[1]]})
    assert r.status_code == 400 and "commitment" in r.text
    assert CV.CommonValueAuctionModule().move_problem({}, 300) == "" and CV.CommonValueAuctionModule().move_problem({}, -1)


def test_the_auction_void_before_the_round(client):
    sid, agents = _game(client, AUCTION, 3, ahead=1000)
    assert _act(client, sid, agents[0], "commit", {"commitment": "a" * 64}).status_code == 400, "no signal yet, no bid"
    assert _act(client, sid, agents[2], "void", {"reason": "late_commit"}).status_code == 200
    assert _outcome(sid)[1]["termination"] == "void"


def test_the_client_reads_its_own_dice_and_signal_from_its_secret(client):
    from emporia import rand_client
    sid, agents = _game(client, DICE, 3)
    sess = rs.load_session(sid)
    s, nonce = RAND_SECRETS[(sid, agents[1])]
    rec = {"s": s, "nonce": nonce, "seat": 1}
    view = rand_client.private_view(sess, agents[1], rec, fetch=lambda rnd, base: fake_beacon_sig(rnd))
    assert view["dice"] == _dice_of(sid, agents[1], sess) and view["to_move"] == _order(sid)[0]
    waiting = rand_client.private_view({**sess, "config": {**sess["config"], "rand": {
        **sess["config"]["rand"], "commits": {agents[1]: sess["config"]["rand"]["commits"][agents[1]]}}},
        "state": {"data": {"rand": {"commits": {agents[1]: {}}}}}}, agents[1], dict(rec))
    assert "waiting" in waiting
    sid2, agents2 = _game(client, AUCTION, 3)
    s2, n2 = RAND_SECRETS[(sid2, agents2[0])]
    sess2 = rs.load_session(sid2)
    v = rand_client.private_view(sess2, agents2[0], {"s": s2, "nonce": n2, "seat": 0},
                                 fetch=lambda rnd, base: fake_beacon_sig(rnd))
    base, parts, _ = _truth(sid2)
    assert v["signal"] == base + parts[agents2[0]] and v["your_part"] == parts[agents2[0]]


def test_a_named_opponent_makes_liars_dice_a_two_player_game(client):
    """Live run 11-10: a direct Liar's Dice challenge was refused ("for two-player games"), so the agent posted an
    open challenge "for" its opponent instead. A named opponent now sets two seats where the game allows it."""
    a, b, c = (f"np{i}_{uuid.uuid4().hex[:6]}" for i in range(3))
    for x in (a, b, c):
        _register_keyed(client, x)
    r = client.post("/sessions", json={"module_type": DICE, "config": {"opponent": b}}, headers=_hdr(a))
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert rs.load_session(sid)["config"]["players"] == 2
    assert client.post(f"/sessions/{sid}/join", json={"agent_id": c}, headers=_hdr(c)).status_code != 200
    assert client.post(f"/sessions/{sid}/join", json={"agent_id": b}, headers=_hdr(b)).status_code == 200
    r = client.post("/sessions", json={"module_type": DICE, "config": {"opponent": b, "players": 4}}, headers=_hdr(a))
    assert r.status_code == 400 and "two-player" in r.text
    r = client.post("/sessions", json={"module_type": AUCTION, "config": {"opponent": b}}, headers=_hdr(a))
    assert r.status_code == 400 and "open challenge" in r.text
    # players is coerced before it is compared: "2" and 2.0 are two; "4" and junk are not
    for ok_value in ("2", 2.0):
        r = client.post("/sessions", json={"module_type": DICE, "config": {"opponent": b, "players": ok_value}}, headers=_hdr(a))
        assert r.status_code == 200, (ok_value, r.text)
    for bad in ("4", "many"):
        r = client.post("/sessions", json={"module_type": DICE, "config": {"opponent": b, "players": bad}}, headers=_hdr(a))
        assert r.status_code == 400 and "two-player" in r.text, bad
    # a game that names no player count says so, instead of "needs more players (at least 3)"
    from emporia.routes import sessions as S
    mod = S.get_interaction_module(AUCTION)
    mod_file = S.sys.modules.get(mod.__module__)
    old = getattr(mod_file, "BOUNDS", None)
    try:
        mod_file.BOUNDS = {}
        r = client.post("/sessions", json={"module_type": AUCTION, "config": {"opponent": b}}, headers=_hdr(a))
    finally:
        if old is None:
            del mod_file.BOUNDS
        else:
            mod_file.BOUNDS = old
    assert r.status_code == 400 and "no player count" in r.text
