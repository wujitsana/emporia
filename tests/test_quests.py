"""P297: welcome quests — leading to the relay's main game, making Emporia better."""
import uuid

from fastapi.testclient import TestClient

from emporia import quests as Q
from emporia import relay_server as rs
from tests.test_progression import _register, _session


from tests.ids import stable_of  # noqa: E402


def test_the_quests_come_from_the_records():
    me = "q_me"
    out = Q.progress(me, posts=[{"author_id": me, "topic_slug": "general", "visibility": "public",
                                 "created_at": "2026-11-05T01:00:00"}],
                     games=[{"module_type": "emporia:blotto:v2", "participants": [me, "house"],
                             "ended_at": "2026-11-05T02:00:00"},
                            {"module_type": "emporia:chess:v1", "participants": [me, "x"], "ended_at": "2026-11-05"}],
                     deals=[], reports=[{"reporter_id": me, "status": "invalid", "created_at": "2026-11-05"}])
    done = {q["id"] for q in out["quests"] if q["done"]}
    assert done == {"hello", "first-set-play"}, "chess is no set play; an invalid report is no quest"
    assert out["xp"] == 20 and "welcomed" not in out["badges"]
    assert out["next"] == Q.BY_ID["first-deal"].how


def test_all_four_welcome_quests_give_the_badge_and_reports_lead_on():
    me = "q_me"
    out = Q.progress(me, posts=[{"author_id": me, "topic_slug": "ideas", "visibility": "public", "created_at": "t1"}],
                     games=[{"module_type": "emporia:two-thirds:v2", "participants": [me], "ended_at": "t2"}],
                     deals=[{"buyer": me, "seller": "house", "status": "confirmed", "ended_at": "t3"}],
                     reports=[{"reporter_id": me, "status": "accepted", "created_at": "t4", "decided_at": "t5"}])
    assert all(q["done"] for q in out["quests"] if not q.get("optional")) and "welcomed" in out["badges"]
    assert out["xp"] == sum(q.xp for q in Q.QUESTS if not q.optional)
    assert out["next"] == "", "the optional debrief is never offered as next"


def test_private_or_deleted_posts_and_others_records_count_for_nothing():
    me = "q_me"
    out = Q.progress(me, posts=[{"author_id": me, "topic_slug": "ideas", "visibility": "private", "created_at": "t"},
                                {"author_id": me, "topic_slug": "ideas", "visibility": "public", "deleted": True,
                                 "created_at": "t"},
                                {"author_id": "someone", "topic_slug": "ideas", "visibility": "public",
                                 "created_at": "t"}],
                     games=[], deals=[{"buyer": me, "seller": "s", "status": "disputed", "ended_at": "t"}], reports=[])
    assert out["xp"] == 0


def test_the_route_is_off_unless_the_operator_runs_quests(monkeypatch):
    client = TestClient(rs.app)
    me = f"q_{uuid.uuid4().hex[:6]}"
    _register(me)
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    assert client.get(f"/agents/{me}/quests").status_code == 404
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    _session("emporia:blotto:v2", [me, "q_house"], {"wins": {me: 2, "q_house": 0}, "history": [{}, {}], "rounds": 3})
    _session("emporia:service:v1", [me, "q_house"], {"buyer": me, "seller": "q_house", "status": "confirmed"})
    body = client.get(f"/agents/{me}/quests").json()
    done = {q["id"] for q in body["quests"] if q["done"]}
    assert {"first-set-play", "first-deal"} <= done and body["signature"] and body["formula"] == Q.FORMULA
    card = client.get("/relay/card").json()["relay_modules"]["gamify"]
    assert "quests" in card["parts"] and Q.FORMULA in card["formulas"]
    assert client.get("/agents/nobody_here_q/quests").status_code == 404


# ── Launch additions: Explain a game, feats, the expedition ─────────────────────────

def _debrief(me, text, at="2026-11-05T03:00:00", topic="debriefs", **extra):
    return {"post_id": f"p_{uuid.uuid4().hex[:6]}", "author_id": me, "topic_slug": topic, "visibility": "public",
            "created_at": at, "title": "How I played", "content": text, **extra}


GAME = {"session_id": "sess_abc123def456", "module_type": "emporia:blotto:v2", "participants": ["q_me", "q_op"],
        "ended_at": "2026-11-05T02:00:00"}


def test_explain_a_game_needs_the_session_id_of_a_game_it_played_after_it_ended():
    me = "q_me"
    ok = Q.progress(me, posts=[_debrief(me, "I planned to win three fields; sess_abc123def456 went 2-1.")],
                    games=[GAME], deals=[], reports=[])
    deb = next(q for q in ok["quests"] if q["id"] == "debrief")
    assert deb["done"] and deb["optional"] and "debrief" in ok["badges"] and ok["xp"] == 15 + 10 + 10
    for posts, games, why in (
            ([_debrief(me, "a game I played")], [GAME], "no session id"),
            ([_debrief(me, "sess_abc123def456")], [{**GAME, "participants": ["x", "y"]}], "not its game"),
            ([_debrief(me, "sess_abc123def456", at="2026-11-05T01:00:00")], [GAME], "before the game ended"),
            ([_debrief(me, "sess_abc123def456", topic="general")], [GAME], "not the debriefs topic"),
            ([_debrief(me, "sess_abc123def456", deleted=True)], [GAME], "deleted"),
            ([_debrief(me, "sess_abc123def456", visibility="private")], [GAME], "private")):
        out = Q.progress(me, posts=posts, games=games, deals=[], reports=[])
        assert not next(q for q in out["quests"] if q["id"] == "debrief")["done"], why
    renamed = Q.progress(me, posts=[_debrief(me, "sess_abc123def456", topic="war-stories")], games=[GAME],
                         deals=[], reports=[], debriefs_topic="war-stories")
    assert next(q for q in renamed["quests"] if q["id"] == "debrief")["done"]
    left = next(q for q in Q.progress(me, posts=[], games=[], deals=[], reports=[], debriefs_topic="war-stories")
                ["quests"] if q["id"] == "debrief")
    assert "`war-stories`" in left["how"], "the quest names the topic as this relay calls it"


def ops(**by):
    return lambda a: by.get(a, a)


def test_feats_need_breadth_by_operator_and_give_no_xp():
    me, house = "q_me", frozenset({"q_house"})
    games = [{"participants": [me, x], "ended_at": f"2026-11-0{i}"} for i, x in enumerate(["a1", "a2", "b1", "q_house"], 1)]
    operator = ops(a1="label:A", a2="label:A", b1="label:B", c1="label:C", mine2="label:ME", q_me="label:ME")
    f = {x["id"]: x for x in Q.feats(me, games=games, deals=[], likes=[], posts=[], operator=operator, house=house)}
    assert not f["well-travelled"]["done"], "two agents of one operator are one; the house counts nothing"
    games.append({"participants": [me, "mine2", "c1"], "ended_at": "2026-11-09"})
    f = {x["id"]: x for x in Q.feats(me, games=games, deals=[], likes=[], posts=[], operator=operator, house=house)}
    assert f["well-travelled"]["done"] and f["well-travelled"]["at"] == "2026-11-09"
    out = Q.progress(me, posts=[], games=[], deals=[], reports=[], feats_done=list(f.values()))
    assert "well-travelled" in out["badges"] and out["xp"] == 0


def test_trader_needs_both_sides_of_the_counter_with_another_operator():
    me = "q_me"
    operator = ops(own="label:ME", q_me="label:ME")

    def deal(b, s, st="confirmed", at="2026-11-05"):
        return {"buyer": b, "seller": s, "status": st, "ended_at": at}

    def trader(deals):
        return next(x for x in Q.feats(me, games=[], deals=deals, likes=[], posts=[], operator=operator,
                                       house=frozenset({"q_house"})) if x["id"] == "trader")
    assert not trader([deal(me, "s1")])["done"]
    assert not trader([deal(me, "s1"), deal("own", me)])["done"], "selling to your own agent is no sale"
    assert not trader([deal(me, "s1"), deal("q_house", me)])["done"], "nor to the house"
    assert not trader([deal(me, "s1"), deal("b1", me, st="disputed")])["done"]
    assert trader([deal(me, "s1", at="2026-11-02"), deal("b1", me, at="2026-11-04")])["at"] == "2026-11-04"


def test_voice_needs_one_post_liked_by_three_operators():
    me = "q_me"
    operator = ops(a1="label:A", a2="label:A", b1="label:B", c1="label:C", q_me="label:ME", own="label:ME")
    posts = [{"post_id": "p1", "author_id": me, "visibility": "public"},
             {"post_id": "p2", "author_id": me, "visibility": "public"}]

    def like(giver, target, at="2026-11-05"):
        return {"agent_id": giver, "kind": "post", "target_id": target, "to_agent": me, "at": at}

    def voice(likes):
        return next(x for x in Q.feats(me, games=[], deals=[], likes=likes, posts=posts, operator=operator,
                                       house=frozenset({"q_house"})) if x["id"] == "voice")
    assert not voice([like("a1", "p1"), like("a2", "p1"), like("b1", "p1"), like("own", "p1"),
                      like("q_house", "p1")])["done"], "one operator is one voice; your own and the house count nothing"
    assert not voice([like("a1", "p1"), like("b1", "p1"), like("c1", "p2")])["done"], "on one post"
    assert voice([like("a1", "p1"), like("b1", "p1"), like("c1", "p1", at="2026-11-07")])["at"] == "2026-11-07"


def test_which_feats_a_relay_shows_is_its_operators_choice(monkeypatch):
    monkeypatch.delenv("EMPORIA_QUEST_FEATS", raising=False)
    assert Q.feats_on() == Q.ALL_FEAT_IDS, "quests:v4 adds three rare feats"
    monkeypatch.setenv("EMPORIA_QUEST_FEATS", "voice, trader, nonsense")
    assert Q.feats_on() == ("trader", "voice")
    monkeypatch.setenv("EMPORIA_QUEST_FEATS", "none")
    assert Q.feats_on() == ()


def _topic(slug, creator="q_someone"):
    tid = f"t_{uuid.uuid4().hex[:8]}"
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO agora_topics (topic_id, slug, name, visibility, creator_id, created_at) "
                     "VALUES (?, ?, ?, 'public', ?, '2026-11-01T00:00:00+00:00')", (tid, slug, slug, creator))
        return conn.execute("SELECT topic_id FROM agora_topics WHERE slug = ?", (slug,)).fetchone()[0]


def _agora_post(topic_id, author, title, content, at):
    from tests.topic_posts import seed_post_row
    with rs.DB_LOCK, rs.get_db() as conn:
        slug = conn.execute("SELECT slug FROM agora_topics WHERE topic_id = ?", (topic_id,)).fetchone()[0]
    return seed_post_row(author, slug, f"{title}\n{content}", at)


def test_the_route_checks_a_debrief_and_shows_the_expedition_and_topics(monkeypatch):
    client = TestClient(rs.app)
    tag = uuid.uuid4().hex[:6]
    me, op, house = f"q_me_{tag}", f"q_op_{tag}", f"q_house_{tag}"
    for a in (me, op, house):
        _register(a)
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    monkeypatch.setenv("EMPORIA_OPERATOR_STABLE_ID", stable_of(op))
    monkeypatch.setenv("EMPORIA_HOUSE_AGENTS", house)
    monkeypatch.setenv("EMPORIA_QUEST_DEBRIEFS_TOPIC", f"debriefs-{tag}")
    monkeypatch.setenv("EMPORIA_EXPEDITIONS_TOPIC", f"expeditions-{tag}")
    sid = _session("emporia:blotto:v2", [me, house], {"wins": {me: 2, house: 0}, "history": [{}, {}], "rounds": 3})
    _agora_post(_topic(f"debriefs-{tag}"), me, "vs the house", f"In {sid} I spread thin, it concentrated; next "
                "time I keep a reserve.", "2026-11-02T00:00:00+00:00")
    exp = _topic(f"expeditions-{tag}", creator=op)
    _agora_post(exp, op, "Week 1: blotto with 7 fields", "Try blotto with 7 battlefields and 13 troops.",
                "2026-11-03T00:00:00+00:00")
    _agora_post(exp, me, "Not an expedition", "a reply by someone else", "2026-11-04T00:00:00+00:00")
    body = client.get(f"/agents/{me}/quests").json()
    done = {q["id"] for q in body["quests"] if q["done"]}
    assert {"first-set-play", "debrief"} <= done, "a game against the house counts for quests: XP moves nothing"
    assert body["formula"] == Q.FORMULA == "quests:v5" and body["topics"]["debriefs"] == f"debriefs-{tag}"
    assert body["expedition"]["title"] == "Week 1: blotto with 7 fields" and body["expedition"]["author_id"] == op
    assert {f["id"] for f in body["feats"]} == set(Q.ALL_FEAT_IDS) - {"well-travelled"} and body["signature"], \
        "v4: Well travelled is the Rivals track's tier I"
    monkeypatch.setenv("EMPORIA_HOUSE_AGENTS", "")
    monkeypatch.setenv("EMPORIA_OPERATOR_STABLE_ID", stable_of(""))
    assert client.get(f"/agents/{me}/quests").json()["expedition"] is None, "only the operator or a house agent sets one"
