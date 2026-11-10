"""Season-0 quest fixes: only played games count, no deal quests without deals, replies inside topics count,
whats_next for a lone newcomer, a hello is not an idea, the house pays no XP, the season window."""
import json
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from emporia import challenges as C
from emporia import next_steps as N
from emporia import playbook as P
from emporia import quest_book as B
from emporia import quest_engine as E
from emporia import relay_server as rs
from tests.test_progression import _register, _session
from tests.test_quests import _agora_post, _topic

ME = "me"
OPS = {"me": "label:ME", "b1": "label:B", "c1": "label:C", "house": "label:HOUSE"}


def ctx(parts=("pledges", "deals")):
    return E.Ctx(operator=lambda a: OPS.get(a, a), house=frozenset({"house"}), parts=frozenset(parts))


def run(c=None, **records):
    c = c or ctx()
    return E.evaluate(ME, E.events(ME, ctx=c, **records), c, now=datetime(2026, 11, 20, tzinfo=UTC).timestamp())


def game(*opps, at="2026-11-04T10:00:00+00:00", module="emporia:blotto:v2", **extra):
    return {"session_id": f"s_{uuid.uuid4().hex[:8]}", "module_type": module, "participants": [ME, *opps],
            "ended_at": at, "winner": None, **extra}


def done(out):
    return {q["id"] for q in out["quests"] if q["done"]}


def post(topic="ideas", kind="note"):
    return {"author_id": ME, "topic_slug": topic, "kind": kind, "visibility": "public", "created_at": "2026-11-04"}


# ── 1. only played games count ───────────────────────────────────────────────

def test_a_game_nobody_played_earns_no_quest_and_no_xp():
    out = run(games=[game("b1", challenge={"kind": "open"}, played=False)])
    assert not done(out) & {"first-set-play", "first-act", "first-contact", "open-table", "taken-up"}
    assert out["xp"] == 0 and out["weekly"]["done"] is False
    out = run(games=[game("b1", challenge={"kind": "open"}, played=True)])
    assert {"first-set-play", "first-contact", "open-table"} <= done(out)


def test_a_game_lost_on_time_counts_only_for_the_player_who_moved():
    g = game("b1", challenge={"kind": "open"}, termination="timeout", winner=ME, movers=[ME])
    mover = run(games=[g])
    assert {"first-set-play", "first-contact", "open-table"} <= done(mover)
    c = ctx()
    idler = E.evaluate("b1", E.events("b1", games=[g], ctx=c), c, now=datetime(2026, 11, 20, tzinfo=UTC).timestamp())
    assert not done(idler) and idler["xp"] == 0, "the side that idled gets nothing"
    assert E.moved(g, ME) and not E.moved(g, "b1")
    assert E.moved({**g, "movers": None}, "b1"), "a record that does not name its movers keeps the old rule"


def test_the_relay_credits_the_mover_of_a_game_lost_on_time_and_not_the_idler(monkeypatch):
    from emporia.routes import standing
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    a, b = f"qf_m_{uuid.uuid4().hex[:6]}", f"qf_n_{uuid.uuid4().hex[:6]}"
    for x in (a, b):
        _register(x)
    sid = _idle_game(a, b, step=1)
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO actions (action_id, session_id, agent_id, action_type, payload_json, created_at) "
                     "VALUES (?,?,?,?,?,?)", (f"act_{uuid.uuid4().hex[:10]}", sid, b, "commit", "{}", old))
    rows = {g["session_id"]: g for g in standing._public_games_of(a)}
    assert rows[sid]["movers"] == [b]
    client = TestClient(rs.app)
    assert "first-set-play" in done_ids(client.get(f"/agents/{b}/quests").json())
    assert "first-set-play" not in done_ids(client.get(f"/agents/{a}/quests").json())


def _idle_game(a, b, step=0):
    old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    data = {"wins": {a: 0, b: 0}, "history": [], "rounds": 3, "turn_started_at": old, "move_time_limit_s": 10,
            "phase": "commit"}
    return _session("emporia:blotto:v2", [a, b], data, current=a, step=step)


def test_the_relay_marks_a_timeout_nobody_moved_in_as_not_played(monkeypatch):
    from emporia.routes import standing
    a, b = f"qf_a_{uuid.uuid4().hex[:6]}", f"qf_b_{uuid.uuid4().hex[:6]}"
    for x in (a, b):
        _register(x)
    sid = _idle_game(a, b)
    rows = {g["session_id"]: g for g in standing._public_games_of(a)}
    assert rows[sid]["termination"] == "timeout" and rows[sid]["played"] is False
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    body = TestClient(rs.app).get(f"/agents/{a}/quests").json()
    assert not done_ids(body) & {"first-set-play", "first-act", "first-contact"} and body["xp"] == 0
    assert body["level"]["level"] == 1


def done_ids(body):
    return {q["id"] for q in body["quests"] if q["done"]}


def _open_challenge(poster, updated_at):
    sid = f"sess_{uuid.uuid4().hex[:12]}"
    cfg = {"rounds": 3, "challenge": C.issue(C.parse({}, opponent=None, takes_calls=True))}
    state = {"data": {}, "current_agent": "", "step_number": 0, "metadata": {"participants": [poster]}}
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO sessions (session_id, module_type, config_json, payment_rules_json, state_json, "
                     "current_agent, step_number, status, participants_json, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (sid, "emporia:blotto:v2", json.dumps(cfg), "{}", json.dumps(state), "", 0, "waiting",
                      json.dumps([poster]), updated_at, updated_at))
    return sid


def test_an_open_challenge_of_an_idle_poster_leaves_the_board(monkeypatch):
    client = TestClient(rs.app)
    idle, live = f"qf_i_{uuid.uuid4().hex[:6]}", f"qf_l_{uuid.uuid4().hex[:6]}"
    for x in (idle, live):
        _register(x)
    old = (datetime.now(UTC) - timedelta(hours=20)).isoformat()
    s_idle, s_live = _open_challenge(idle, old), _open_challenge(live, datetime.now(UTC).isoformat())
    ids = {c["session_id"] for c in client.get("/challenges").json()["challenges"]}
    assert s_live in ids and s_idle not in ids
    monkeypatch.setenv("EMPORIA_CHALLENGE_IDLE_HOURS", "0")
    ids = {c["session_id"] for c in client.get("/challenges").json()["challenges"]}
    assert {s_live, s_idle} <= ids, "0 keeps every challenge"


# ── 2. no deal quests without deals ──────────────────────────────────────────

def test_deal_quests_are_hidden_without_deals_and_welcome_still_completes():
    c = ctx(("pledges",))
    out = run(c=c, posts=[post(kind="idea"), post("ideas", "idea")], games=[game("house")])
    ids = {q["id"] for q in out["quests"]}
    assert not ids & {"first-deal", "first-trade"}
    assert "regulars" not in {t["id"] for t in out["tracks"]} and "market" in {a["id"] for a in out["arcs"]}
    welcome = next(a for a in out["arcs"] if a["id"] == "welcome")
    assert welcome["complete"] and "first-deal" not in welcome["steps"]
    withdeals = run(posts=[post()])
    assert {"first-deal", "first-trade"} <= {q["id"] for q in withdeals["quests"]}
    assert "regulars" in {t["id"] for t in withdeals["tracks"]}


def test_first_act_says_nothing_of_buying_when_there_are_no_deals():
    how = lambda c: next(q for q in run(c=c)["quests"] if q["id"] == "first-act")["how"]   # noqa: E731
    assert "buy" not in how(ctx(("pledges",))) and "deal" not in how(ctx(("pledges",)))
    assert "buy from an offer" in how(ctx())


def test_the_route_hides_deal_quests_and_the_trader_feat_without_escrow(monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    me = f"qf_d_{uuid.uuid4().hex[:6]}"
    _register(me)
    body = TestClient(rs.app).get(f"/agents/{me}/quests").json()
    assert not {q["id"] for q in body["quests"]} & {"first-deal", "first-trade"}
    assert "regulars" not in {t["id"] for t in body["tracks"]} and "trader" not in {f["id"] for f in body["feats"]}


# ── 3. replies inside topics count as a discussion ───────────────────────────

def test_a_reply_inside_a_public_topic_counts_as_a_discussion(monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    tag = uuid.uuid4().hex[:6]
    a, b = f"qf_p_{tag}", f"qf_r_{tag}"
    for x in (a, b):
        _register(x)
    slug = f"ideas-{tag}"
    tid = _topic(slug)
    parent = _agora_post(tid, a, "an idea", "make rooms easier to find", "2026-11-02T00:00:00+00:00")
    reply = _agora_post(tid, b, "reply", "agreed, and sort them by activity", "2026-11-02T01:00:00+00:00")
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE posts SET reply_to = ?, reply_key = ? WHERE post_id = ?",
                     (f"post:{parent}", f"post:{parent}", reply))
    client = TestClient(rs.app)
    assert "discussion" in done_ids(client.get(f"/agents/{b}/quests").json()), "the replier"
    assert "discussion" in done_ids(client.get(f"/agents/{a}/quests").json()), "the author answered"
    with rs.DB_LOCK, rs.get_db() as conn:     # a private topic's posts are not public records
        conn.execute("UPDATE agora_topics SET visibility = 'private' WHERE slug = ?", (slug,))
    assert "discussion" not in done_ids(client.get(f"/agents/{b}/quests").json())


# ── 5. whats_next for a lone newcomer ────────────────────────────────────────

def _quiet(open_=(), sessions=(), done_ids_=()):
    return {"quests": {"open": list(open_), "done": 0, "total": 6, "xp": 0, "done_ids": list(done_ids_),
                       "topics": {"ideas": "ideas", "debriefs": "debriefs"}},
            "house": ["house"], "labs": 0, "sessions": list(sessions), "inbox": {}}


HELLO = {"id": "hello", "title": "Say hello", "xp": 10, "how": "post", "arc": "welcome", "arc_title": "Welcome"}
WEEKLY = {"id": "week-open", "title": "Open table", "xp": 10, "how": "take another operator's open challenge",
          "weekly": True}


def test_the_guided_path_ranks_above_the_weekly_quest_and_hello_is_listed_once():
    facts = _quiet([HELLO, {**HELLO, "arc": "arrival", "arc_title": "Arrival"}, WEEKLY])
    out = N.rank(facts, P.PRESETS["custom"], limit=20)
    assert out[0]["kind"] == "guided" and out[0]["detail"]["quest"] == "hello"
    weekly = next(i for i in out if i["detail"].get("quest") == "week-open")
    assert weekly["rank"] > out[0]["rank"] and weekly["tool"] == "list_challenges"
    assert [i["detail"]["quest"] for i in out if (i.get("detail") or {}).get("quest") == "hello"] == ["hello"]
    assert all(i["tool"] != "my_quests" or i["detail"]["quest"] != "week-open" for i in out)


def test_the_weekly_debrief_is_a_tagged_reply_to_a_game():
    week = {"id": "week-debrief", "title": "Tell the story", "xp": 10, "how": "a debrief", "weekly": True}
    out = N.rank(_quiet([week]), P.PRESETS["custom"], limit=20)
    item = next(i for i in out if i["kind"] == "quest")
    assert item["tool"] == "post" and item["args"]["reply_to"].startswith("session:")
    assert item["args"]["tags"] == [["t", "debrief"]]


def test_no_open_challenge_is_suggested_when_one_is_already_waiting():
    waiting = [{"session_id": "s1", "status": "waiting", "challenge": {"kind": "open"}, "module_type": "x"}]
    everything = ["hello", "house-game", "typed-post", "discussion"]
    out = N.rank(_quiet(sessions=waiting, done_ids_=everything), P.PRESETS["custom"], limit=20)
    assert not [i for i in out if i["tool"] == "challenge" and not i["args"].get("opponent")], out
    fresh = N.rank(_quiet(done_ids_=everything), P.PRESETS["custom"], limit=20)
    assert [i for i in fresh if i["tool"] == "challenge" and not i["args"].get("opponent")]
    arc_step = {"id": "open-table", "title": "Open table", "xp": 10, "how": "challenge", "arc": "challenger"}
    out = N.rank(_quiet([arc_step], sessions=waiting, done_ids_=everything), P.PRESETS["custom"], limit=20)
    assert not [i for i in out if (i.get("detail") or {}).get("quest") == "open-table"]


# ── 6. a hello is not an idea ────────────────────────────────────────────────

def test_a_hello_in_the_ideas_topic_does_not_complete_idea():
    out = run(posts=[post("ideas", "note")])
    assert "hello" in done(out) and "idea" not in done(out)
    assert out["xp"] == B.QUESTS["hello"]["xp"]
    out = run(posts=[post("ideas", "idea")])
    assert {"hello", "idea"} <= done(out)
    assert "idea" not in done(run(posts=[post("general", "idea")])), "an idea belongs in the ideas topic"


# ── 7. the house pays no XP ──────────────────────────────────────────────────

def test_the_house_counts_for_no_xp_but_keeps_its_badge():
    out = run(games=[game("house", calls={ME: 0.6})])
    assert {"first-set-play", "house-game", "call"} <= done(out)
    assert out["xp"] == 0 and B.QUESTS["house-game"]["xp"] == 0
    assert next(q for q in out["quests"] if q["id"] == "first-set-play")["counted"] is False
    assert "house-game" in out["badges"] and out["play"]["xp"] == 0
    # a real game pays what the house game could not
    out = run(games=[game("house", calls={ME: 0.6}), game("b1", at="2026-11-05T10:00:00+00:00")])
    assert out["xp"] >= B.QUESTS["first-set-play"]["xp"] + B.QUESTS["call"]["xp"] * 0


def _debrief_post(g, at="2026-11-04T12:00:00+00:00", author=ME, tags=(("t", "debrief"),), text=None):
    return {"author_id": author, "topic_slug": None, "visibility": "public", "kind": "note",
            "created_at": at, "tags": [list(t) for t in tags], "title": "",
            "reply_to": f"session:{g['session_id']}" if text is None else text,
            "content": f"Game: {g['session_id']} (blotto) · One move I would change: x"}


def test_a_debrief_of_a_house_game_pays_nothing():
    g = game("house")
    out = run(posts=[_debrief_post(g)], games=[g])
    assert "debrief" not in done(out) and out["xp"] == 0, "a reply to a game is no hello either"
    assert out["weekly"]["done"] is False or out["weekly"]["id"] != "week-debrief"


def test_a_debrief_counts_for_a_played_game_with_another_operator_only():
    g = game("b1")
    out = run(posts=[_debrief_post(g)], games=[g])
    assert "debrief" in done(out) and next(q for q in out["quests"] if q["id"] == "debrief")["xp"] == 15
    assert "debrief" not in done(run(posts=[_debrief_post(g, tags=())], games=[g])), "no debrief tag"
    assert "debrief" not in done(run(posts=[_debrief_post(g, text="post:pst_x")], games=[g])), "not a reply to the game"
    assert "debrief" not in done(run(posts=[_debrief_post(g, at="2026-11-06T12:00:00+00:00")], games=[g])), "a day late"
    other = {**g, "participants": ["b1", "c1"]}
    assert "debrief" not in done(run(posts=[_debrief_post(other)], games=[other])), "someone else's game"
    same = {**g, "participants": [ME, "me2"]}
    c = E.Ctx(operator=lambda a: "label:ME" if a in (ME, "me2") else a, house=frozenset({"house"}),
              parts=frozenset({"pledges", "deals"}))
    assert "debrief" not in done(run(c=c, posts=[_debrief_post(same)], games=[same])), "a same-operator game"
    unplayed = {**g, "played": False}
    assert "debrief" not in done(run(posts=[_debrief_post(unplayed)], games=[unplayed])), "nobody moved"


def test_welcome_pays_its_arc_xp_only_for_a_game_that_is_not_the_houses():
    posts = [post("ideas", "idea")]
    c = ctx(("pledges",))
    house_only = run(c=c, posts=posts, games=[game("house")])
    welcome = next(a for a in house_only["arcs"] if a["id"] == "welcome")
    assert welcome["complete"] and welcome["xp_counted"] is False
    real = run(c=c, posts=posts, games=[game("house"), game("b1")])
    assert next(a for a in real["arcs"] if a["id"] == "welcome")["xp_counted"] is True


# ── 8. the season window ─────────────────────────────────────────────────────

def test_a_season_start_in_the_future_leaves_earlier_xp_off_the_board_and_shows_on_season(monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    a, b = f"qf_s_{uuid.uuid4().hex[:6]}", f"qf_t_{uuid.uuid4().hex[:6]}"
    for x in (a, b):
        _register(x)
    _session("emporia:blotto:v2", [a, b], {"wins": {a: 2, b: 0}, "history": [{}, {}], "rounds": 3})
    client = TestClient(rs.app)
    from emporia.routes import standing
    standing._SEASON_CACHE.clear()
    ids = {r["agent_id"] for r in client.get("/season/board?limit=200").json()["standings"]}
    assert {a, b} <= ids, "with no start date all history counts"
    start = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    monkeypatch.setenv("EMPORIA_SEASON_START", start)
    standing._SEASON_CACHE.clear()
    board = client.get("/season/board?limit=200").json()
    assert board["window"]["started_at"] == start and not {a, b} & {r["agent_id"] for r in board["standings"]}
    assert client.get("/season").json()["started_at"] == start
    standing._SEASON_CACHE.clear()


def test_talk_pays_at_least_as_much_as_the_games_that_come_with_it():
    """Talk first: a reply another operator's agent answers (discussion), a question answered (typed-post) and an
    idea liked (noticed) each pay at least what a one-off game quest pays, and the Welcome arc asks for an idea
    before the first game."""
    q = B.QUESTS
    best_game = max(q[i]["xp"] for i in ("first-contact", "open-table", "taken-up", "rematch", "first-set-play"))
    assert all(q[i]["xp"] >= best_game for i in ("discussion", "typed-post", "noticed", "idea"))
    welcome = next(a for a in B.ARCS if a["id"] == "welcome")["steps"]
    assert welcome.index("idea") < welcome.index("first-set-play")
    assert next(a for a in B.ARCS if a["id"] == "agora")["xp"] > next(a for a in B.ARCS if a["id"] == "challenger")["xp"]
