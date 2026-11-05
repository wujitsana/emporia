"""P288: the gamify module's sheet — Craft per game (Glicko-2), Trade, Trust, levels, class."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from emporia import progression as pg
from emporia import relay_server as rs
from emporia.routes import standing as st

DAY = 86_400


def game(a, b, winner, day, module="emporia:blotto:v1", **extra):
    return {"session_id": f"s{uuid.uuid4().hex[:8]}", "module_type": module, "participants": [a, b],
            "winner": winner, "ended_at": day * DAY, **extra}


def test_glicko2_matches_glickmans_worked_example():
    r = pg.Rating("a", "m", 1500, 200, 0.06)
    opp = lambda x, d: pg.Rating("o", "m", x, d)  # noqa: E731
    rating, rd, vol = pg._update(r, 200 / pg.SCALE, [(opp(1400, 30), 1, 1), (opp(1550, 100), 0, 1),
                                                     (opp(1700, 300), 0, 1)])
    assert round(rating, 1) == 1464.1 and round(rd, 1) == 151.5 and round(vol, 5) == 0.06


def test_winning_raises_losing_lowers_and_a_draw_between_equals_holds():
    t = pg.craft_ratings([game("a", "b", "a", 1)])["emporia:blotto:v1"]
    assert t["a"].rating > 1500 > t["b"].rating and t["a"].games == t["b"].games == 1
    d = pg.craft_ratings([game("a", "b", None, 1, termination="rounds")])["emporia:blotto:v1"]
    assert round(d["a"].rating) == round(d["b"].rating) == 1500 and d["a"].rd < pg.START_RD


def test_the_same_opponent_again_and_again_flattens():
    """Ten wins over one agent weigh less than ten wins over ten agents."""
    one = pg.craft_ratings([game("a", "b", "a", i) for i in range(10)])["emporia:blotto:v1"]["a"]
    ten = pg.craft_ratings([game("a", f"b{i}", "a", i) for i in range(10)])["emporia:blotto:v1"]["a"]
    assert ten.rating > one.rating and one.opponents == 1 and ten.opponents == 10


def test_a_game_nobody_played_is_no_result():
    games = [game("a", "b", None, 1, termination="timeout"), game("a", "b", None, 1, termination="no_reveals"),
             game("a", "b", "z", 1)]                                   # a winner who did not play
    assert pg.craft_ratings(games) == {}


def test_a_many_player_win_counts_as_one_game():
    many = pg.craft_ratings([{**game("a", "b", "a", 1, module="emporia:two_thirds:v1"),
                              "participants": ["a", "b", "c", "d", "e"]}])["emporia:two_thirds:v1"]
    two = pg.craft_ratings([game("a", "b", "a", 1)])["emporia:blotto:v1"]
    assert abs(many["a"].rating - two["a"].rating) < 60
    assert many["b"].rating == many["c"].rating < 1500       # losers move against the winner only


def test_tied_players_share_the_win():
    t = pg.craft_ratings([{**game("a", "b", None, 1, module="emporia:two_thirds:v1"),
                           "participants": ["a", "b", "c"], "termination": "tie", "tied": ["a", "b"]}])
    t = t["emporia:two_thirds:v1"]
    assert t["a"].rating == t["b"].rating > 1500 > t["c"].rating


def test_a_stake_counts_a_little_more_but_not_by_its_size():
    free = pg.craft_ratings([game("a", "b", "a", 1)])["emporia:blotto:v1"]["a"].rating
    small = pg.craft_ratings([game("a", "b", "a", 1, stake_per_participant="0.10")])["emporia:blotto:v1"]["a"].rating
    big = pg.craft_ratings([game("a", "b", "a", 1, stake_per_participant="100")])["emporia:blotto:v1"]["a"].rating
    assert small > free and small == big


def test_uncertainty_grows_while_an_agent_does_not_play():
    r = pg.craft_ratings([game("a", f"b{i}", "a", i) for i in range(30)])["emporia:blotto:v1"]["a"]
    assert pg.as_of(r, r.last_at + 365 * DAY).rd > r.rd


def test_the_order_of_records_handed_over_does_not_change_the_result():
    games = [game("a", "b", "a", 1), game("b", "c", "c", 2), game("c", "a", None, 3, termination="rounds")]
    one, two = pg.craft_ratings(games), pg.craft_ratings(list(reversed(games)))
    assert {k: {a: r.rating for a, r in b.items()} for k, b in one.items()} == \
           {k: {a: r.rating for a, r in b.items()} for k, b in two.items()}


def deal(buyer, seller, status, day, paid=True):
    return {"session_id": f"d{uuid.uuid4().hex[:8]}", "buyer": buyer, "seller": seller,
            "status": status, "paid": paid, "ended_at": day * DAY}


def sheet_for(agent, *, games=(), deals=(), abandoned=(), weeks=0):
    return pg.base_sheet(agent, table=pg.craft_ratings(games), deals=deals, abandoned=abandoned,
                         registered_at=0, now=weeks * 7 * DAY)


def test_trade_grows_with_paid_deliveries_to_many_buyers_and_falls_with_missed_ones():
    good = sheet_for("s", deals=[deal(f"b{i}", "s", "confirmed", i) for i in range(10)])
    same = sheet_for("s", deals=[deal("b", "s", "confirmed", i) for i in range(10)])
    free = sheet_for("s", deals=[deal(f"b{i}", "s", "confirmed", i, paid=False) for i in range(10)])
    bad = sheet_for("s", deals=[*[deal(f"b{i}", "s", "confirmed", i) for i in range(10)],
                                deal("x", "s", "timeout_no_delivery", 11)])
    assert good["trade"]["score"] > same["trade"]["score"] > 0
    assert good["trade"]["score"] > free["trade"]["score"]
    assert bad["trade"]["score"] < good["trade"]["score"] and bad["trade"]["not_delivered"] == 1
    assert good["trade"]["sold"] == 10 and good["trust"]["counterparties"] == 10


def test_a_dispute_weighs_on_the_seller_only():
    sh = sheet_for("b", deals=[deal("b", "s", "disputed", 1)])
    assert sh["trade"]["score"] == 0 and sh["trust"]["score"] == 0
    assert sheet_for("s", deals=[deal("b", "s", "disputed", 1)])["trade"]["disputed_as_seller"] == 1


def test_trust_counts_time_and_distinct_others_and_falls_for_a_game_left_on_its_turn():
    plain = sheet_for("a", games=[game("a", f"b{i}", f"b{i}", i) for i in range(4)], weeks=9)
    assert plain["trust"]["counterparties"] == 4 and plain["trust"]["score"] == 2 + 3
    left = sheet_for("a", abandoned=[{"participants": ["a", "b"], "current_agent": "a", "step_number": 3}],
                     weeks=9)
    other = sheet_for("a", abandoned=[{"participants": ["a", "b"], "current_agent": "b", "step_number": 3}],
                      weeks=9)
    assert left["trust"]["score"] < other["trust"]["score"] and left["trust"]["abandoned_on_turn"] == 1


def test_class_is_the_top_attribute_and_none_for_a_newcomer():
    assert sheet_for("new")["class"] is None and sheet_for("new")["levels"] == {"craft": 1, "trade": 1, "trust": 1}
    merchant = sheet_for("s", deals=[deal(f"b{i}", "s", "confirmed", i) for i in range(40)])
    assert merchant["class"] == "Merchant" and merchant["levels"]["trade"] > merchant["levels"]["trust"]
    # Beating sixty fresh agents leaves a rating uncertain, by design; a record against
    # ten regular opponents settles it.
    fresh = sheet_for("a", games=[game("a", f"b{i}", "a", i) for i in range(60)])
    assert fresh["craft"][0]["provisional"] is True and fresh["levels"]["craft"] == 1
    strategist = sheet_for("a", games=[game("a", f"b{i % 10}", "a" if i % 3 else f"b{i % 10}", i / 24)
                                       for i in range(60)])
    assert strategist["craft"][0]["provisional"] is False and strategist["class"] == "Strategist"


def test_render_reads_as_a_sheet():
    text = pg.render(sheet_for("a", games=[game("a", "b", "a", 1)]))
    assert text.startswith("a — no class yet") and "blotto" in text and "provisional" in text
    assert "Adjusted" not in text and "Adjusted" in pg.render({**sheet_for("a"), "adjusted": True})


# ── the relay ──────────────────────────────────────────────────────────────


@pytest.fixture
def client():
    return TestClient(rs.app)


def test_nothing_runs_unless_the_operator_turns_it_on(client, monkeypatch):
    monkeypatch.delenv("EMPORIA_GAMIFY", raising=False)
    assert client.get("/ratings", params={"module_type": "emporia:blotto:v1"}).status_code == 404
    assert client.get("/agents/anyone/sheet").status_code == 404
    card = client.get("/relay/card").json()
    assert "gamify" not in card["relay_modules"]
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    card = client.get("/relay/card").json()
    assert card["relay_modules"]["gamify"] == {"v": 1, "parts": ["sheets"], "formulas": [pg.FORMULA]}


def _register(agent):
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO authorized_agents (agent_id, public_key_hex, registered_at) "
                     "VALUES (?, '', '2026-01-01T00:00:00+00:00')", (agent,))


def _session(module, participants, data, status="completed", current="", step=2, metadata=None):
    sid = f"sess_{uuid.uuid4().hex[:12]}"
    state = {"data": data, "current_agent": current, "step_number": step,
             "metadata": metadata or {"participants": participants}}
    with rs.DB_LOCK, rs.get_db() as conn:
        for p in participants:
            conn.execute("INSERT INTO participants (session_id, agent_id, joined_at) VALUES (?, ?, ?)",
                         (sid, p, "2026-11-01T00:00:00+00:00"))
        conn.execute("INSERT INTO sessions (session_id, module_type, config_json, payment_rules_json, state_json, "
                     "current_agent, step_number, status, participants_json, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (sid, module, "{}", "{}", json.dumps(state), current, step, status,
                      json.dumps(participants), "2026-11-01T00:00:00+00:00", "2026-11-01T01:00:00+00:00"))
    return sid


def test_the_sheet_and_ladder_come_from_this_relays_records(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    a, b = f"pg_a_{uuid.uuid4().hex[:6]}", f"pg_b_{uuid.uuid4().hex[:6]}"
    for x in (a, b):
        _register(x)
    won = _session("emporia:blotto:v1", [a, b], {"wins": {a: 2, b: 0}, "history": [{}, {}], "rounds": 3})
    _session("emporia:service:v1", [b, a], {"buyer": b, "seller": a, "status": "confirmed"})
    _session("emporia:blotto:v1", [a, b], {"wins": {a: 0, b: 0}, "history": [], "rounds": 3},
             status="abandoned", current=b, step=1)

    sh = client.get(f"/agents/{a}/sheet").json()
    assert sh["craft"][0]["module_type"] == "emporia:blotto:v1" and sh["craft"][0]["rating"] > 1500
    assert sh["trade"]["sold"] == 1 and sh["sessions_counted"] == 3 and sh["signature"]
    assert sh["trust"]["abandoned_on_turn"] == 0
    assert client.get(f"/agents/{b}/sheet").json()["trust"]["abandoned_on_turn"] == 1
    assert won in [g["session_id"] for g in st._craft_games()]

    ladder = client.get("/ratings", params={"module_type": "emporia:blotto:v1"}).json()["ratings"]
    names = [r["agent_id"] for r in ladder]
    assert names.index(a) < names.index(b)
    rows = client.get("/leaderboard", params={"module_type": "emporia:blotto:v1", "limit": 200}).json()["standings"]
    mine = next(r for r in rows if r["agent_id"] == a)
    assert mine["rating"]["rating"] > 1500 and mine["rating"]["provisional"] is True


def test_a_private_game_is_not_in_anyones_craft(monkeypatch):
    sid = _session("emporia:blotto:v1", ["p1", "p2"], {"wins": {"p1": 2, "p2": 0}, "history": [{}, {}], "rounds": 3})
    monkeypatch.setattr(rs, "_session_visibility", lambda s: "participants" if s["session_id"] == sid else "public")
    assert sid not in [g["session_id"] for g in st._craft_games()]


def test_an_unknown_agent_has_no_sheet(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    assert client.get("/agents/nobody_here_at_all/sheet").status_code == 404


# ── agents gaming their own sheet ──────────────────────────────────────────


def who(**ops):
    """Standing data: name=operator (or name=(operator, verified, registered_day))."""
    out = {}
    for name, v in ops.items():
        op, verified, day = v if isinstance(v, tuple) else (v, True, -100)
        out[name] = pg.Who(operator=op, verified=verified, registered_at=day * DAY)
    return out


def test_your_own_other_agent_is_not_an_opponent_or_a_customer():
    w = who(a="nous:1", sock="nous:1", b="nous:2")
    t = pg.craft_ratings([game("a", "sock", "a", i) for i in range(20)], w)
    assert t == {}                                                     # nothing at all
    sh = pg.base_sheet("a", table={}, deals=[deal("sock", "a", "confirmed", i) for i in range(20)],
                       abandoned=[], registered_at=None, now=0, who=w)
    assert sh["trade"]["score"] == 0 and sh["trade"]["sold"] == 0 and sh["adjusted"] is True


def test_many_agents_of_one_operator_are_one_counterparty():
    flock = {f"b{i}": "nous:farm" for i in range(10)}
    w = who(s="nous:me", **flock, **{f"c{i}": f"nous:{i}" for i in range(10)})
    one = pg.base_sheet("s", table={}, deals=[deal(f"b{i}", "s", "confirmed", i) for i in range(10)],
                        abandoned=[], registered_at=None, now=0, who=w)
    ten = pg.base_sheet("s", table={}, deals=[deal(f"c{i}", "s", "confirmed", i) for i in range(10)],
                        abandoned=[], registered_at=None, now=0, who=w)
    assert one["trust"]["counterparties"] == 1 and ten["trust"]["counterparties"] == 10
    assert one["trade"]["score"] < ten["trade"]["score"] / 2
    games = pg.craft_ratings([game("a", f"b{i}", "a", i) for i in range(10)], {**w, "a": pg.Who("nous:a", True, 0)})
    assert games["emporia:blotto:v1"]["a"].opponents == 1


def test_fresh_and_unverified_agents_count_for_little():
    verified = who(s="nous:me", **{f"b{i}": f"nous:{i}" for i in range(10)})
    keyonly = who(s="nous:me", **{f"b{i}": (f"b{i}", False, -100) for i in range(10)})
    fresh = who(s="nous:me", **{f"b{i}": (f"nous:{i}", True, 0) for i in range(10)})   # registered that day
    deals = [deal(f"b{i}", "s", "confirmed", 0) for i in range(10)]
    score = {name: pg.base_sheet("s", table={}, deals=deals, abandoned=[], registered_at=None, now=0,
                                 who=w)["trade"]["score"] for name, w in
             (("verified", verified), ("keyonly", keyonly), ("fresh", fresh))}
    assert score["keyonly"] <= score["verified"] / 4 + 0.01 and score["fresh"] <= score["verified"] / 10 + 0.01
    beat_verified = pg.craft_ratings([game("a", f"b{i}", "a", 0) for i in range(10)],
                                     {**verified, "a": pg.Who("nous:a", True, -100 * DAY)})
    beat_fresh = pg.craft_ratings([game("a", f"b{i}", "a", 0) for i in range(10)],
                                  {**fresh, "a": pg.Who("nous:a", True, -100 * DAY)})
    assert beat_fresh["emporia:blotto:v1"]["a"].rating < beat_verified["emporia:blotto:v1"]["a"].rating - 100


def test_an_agent_nobody_registered_counts_least():
    assert pg._weight({}, "ghost", 0) == pg.UNVERIFIED_WEIGHT * pg.MIN_RAMP


def test_walking_away_from_a_losing_game_is_a_loss():
    t = pg.craft_ratings([{**game("a", "b", None, 1), "abandoned_by": "a"}])["emporia:blotto:v1"]
    assert t["a"].rating < 1500 < t["b"].rating
    assert pg.craft_ratings([{**game("a", "b", None, 1), "abandoned_by": "z"}]) == {}


def test_free_deals_are_nearly_worthless():
    paid = sheet_for("s", deals=[deal(f"b{i}", "s", "confirmed", i) for i in range(10)])
    free = sheet_for("s", deals=[deal(f"b{i}", "s", "confirmed", i, paid=False) for i in range(10)])
    assert free["trade"]["score"] <= paid["trade"]["score"] / 10 + 0.01


def test_the_relay_weighs_by_operator_and_never_says_who_is_linked(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    a, sock = f"pg_op_{uuid.uuid4().hex[:6]}", f"pg_sock_{uuid.uuid4().hex[:6]}"
    nous = f"nous_{uuid.uuid4().hex[:6]}"
    for x in (a, sock):
        _register(x)
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET nous_user_id = ? WHERE agent_id IN (?, ?)", (nous, a, sock))
    for _ in range(3):
        _session("emporia:blotto:v1", [a, sock], {"wins": {a: 2, sock: 0}, "history": [{}, {}], "rounds": 3})
    _session("emporia:service:v1", [sock, a], {"buyer": sock, "seller": a, "status": "confirmed"})
    body = client.get(f"/agents/{a}/sheet").text
    sh = json.loads(body)
    assert sh["craft"] == [] and sh["trade"]["sold"] == 0 and sh["adjusted"] is True
    assert sock not in body and nous not in body


def test_leaving_a_game_against_your_own_agent_counts_nothing_either_way():
    w = who(a="nous:1", sock="nous:1")
    sh = pg.base_sheet("a", table={}, deals=[], registered_at=None, now=0, who=w,
                       abandoned=[{"participants": ["a", "sock"], "current_agent": "a", "step_number": 9}])
    assert sh["trust"]["abandoned_on_turn"] == 0 and sh["trust"]["score"] == 0 and sh["adjusted"] is True


def test_craft_rates_set_plays_not_games_an_engine_decides():
    """P297: chess is played for the play and the talk; nothing tells an agent's own
    moves from an engine's, so Craft rates the set plays only."""
    from emporia.routes.standing import craft_modules
    rated = craft_modules()
    assert "emporia:chess:v1" not in rated
    assert {"emporia:blotto:v1", "emporia:two-thirds:v1", "emporia:lowest-unique:v1"} <= set(rated)
