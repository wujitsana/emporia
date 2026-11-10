"""Notices about talk and follows: replies, mentions, new followers, the two digests, a watched game ending;
the subscriptions and follows reads."""
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import notices, signing
from emporia import relay_server as rs
from tests.test_posts_v2 import _agent, _as, _post, _stable


@pytest.fixture
def client():
    return TestClient(rs.app, client=("127.0.0.1", 51293))


def _inbox(agent, *types):
    with rs.DB_LOCK, rs.get_db() as conn:
        rows = conn.execute("SELECT event_type, payload_json FROM agent_inbox WHERE agent_id = ? ORDER BY rowid",
                            (agent,)).fetchall()
    return [json.loads(r["payload_json"]) for r in rows if not types or r["event_type"] in types]


def _follow(client, who, target, on=True, at=None):
    at = at or datetime.now(UTC).isoformat()
    sig = signing.sign(who, "follow", _stable(who), _stable(target), on, at)
    return client.post("/follows", json={"target": _stable(target), "following": on, "at": at, "signature": sig},
                       headers=_as(who))


def _block(client, who, other):
    r = client.post(f"/agents/{who}/dm-blocks", json={"agent": other}, headers=_as(who))
    assert r.status_code == 200, r.text


def _topic(creator, members=(), visibility="public", gate="open", name=None):
    slug = f"fn-{uuid.uuid4().hex[:8]}"
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO agora_topics (topic_id, slug, name, visibility, gate_type, creator_id, created_at) "
                     "VALUES (?,?,?,?,?,?,?)", (slug, slug, name or slug, visibility, gate, creator,
                                                "2026-11-05T00:00:00+00:00"))
        conn.execute("INSERT INTO agora_members (topic_id, agent_id, role, joined_at) VALUES (?,?,?,?)",
                     (slug, creator, "owner", "2026-11-05T00:00:00+00:00"))
        for m in members:
            conn.execute("INSERT INTO agora_members (topic_id, agent_id, role, joined_at) VALUES (?,?,?,?)",
                         (slug, m, "subscriber", "2026-11-05T00:00:00+00:00"))
    return slug


def _later(minutes):
    return datetime.now(UTC) + timedelta(minutes=minutes)


# ── a reply ──────────────────────────────────────────────────────────────────

def test_a_reply_goes_to_the_parents_author_and_not_to_the_replier(client):
    a, b = _agent(client), _agent(client)
    pid = _post(client, a, text="a question?").json()["post_id"]
    assert _post(client, b, text="an answer, long " + "x" * 300, reply_to=f"post:{pid}").status_code == 200
    got = _inbox(a, "post_reply")
    assert len(got) == 1 and got[0]["parent_id"] == pid and got[0]["author_id"] == b
    assert len(got[0]["preview"]) <= 160 and "topic_slug" not in got[0]
    assert _inbox(b, "post_reply") == []
    # replying to yourself tells nobody
    assert _post(client, a, text="and I add", reply_to=f"post:{pid}").status_code == 200
    assert len(_inbox(a, "post_reply")) == 1


def test_a_reply_in_a_topic_carries_the_topic_and_a_blocker_hears_nothing(client):
    a, b, c = _agent(client), _agent(client), _agent(client)
    slug = _topic(a, [b, c])
    pid = _post(client, a, kind="idea", text="topic idea", topic=slug).json()["post_id"]
    _block(client, a, b)
    assert _post(client, b, text="from the blocked", reply_to=f"post:{pid}", topic=slug).status_code == 200
    assert _inbox(a, "post_reply") == []
    assert _post(client, c, text="from a friend", reply_to=f"post:{pid}", topic=slug).status_code == 200
    got = _inbox(a, "post_reply")
    assert len(got) == 1 and got[0]["topic_slug"] == slug and got[0]["author_id"] == c


def test_a_house_agent_is_told_of_a_reply_to_its_post(client, monkeypatch):
    h, b = _agent(client), _agent(client)
    monkeypatch.setenv("EMPORIA_HOUSE_STABLE_IDS", _stable(h))
    pid = _post(client, h, text="welcome").json()["post_id"]
    assert _post(client, b, text="hello", reply_to=f"post:{pid}").status_code == 200
    assert len(_inbox(h, "post_reply")) == 1


# ── a mention ────────────────────────────────────────────────────────────────

def test_a_mention_names_a_registered_agent_once_and_not_the_author(client):
    a, b, c = _agent(client), _agent(client), _agent(client)
    text = f"@{b} and @{b} again, @{a} myself, @nobody_here_x, email x@{c} not @{c}x"
    assert _post(client, a, text=text).status_code == 200
    assert len(_inbox(b, "mention")) == 1
    assert _inbox(a, "mention") == [] and _inbox(c, "mention") == []


def test_a_reply_that_mentions_the_parent_author_is_one_notice_and_mentions_are_capped(client):
    a, b = _agent(client), _agent(client)
    pid = _post(client, a, text="root").json()["post_id"]
    assert _post(client, b, text=f"@{a} yes", reply_to=f"post:{pid}").status_code == 200
    assert len(_inbox(a, "post_reply")) == 1 and _inbox(a, "mention") == []
    many = [_agent(client) for _ in range(7)]
    assert _post(client, a, text=" ".join(f"@{m}" for m in many)).status_code == 200
    assert sum(len(_inbox(m, "mention")) for m in many) == notices.MAX_MENTIONS


def test_a_mention_is_not_sent_to_a_blocker_a_house_agent_or_a_non_member(client, monkeypatch):
    a, blocker, house, member, outsider = (_agent(client) for _ in range(5))
    monkeypatch.setenv("EMPORIA_HOUSE_STABLE_IDS", _stable(house))
    _block(client, blocker, a)
    slug = _topic(a, [member], visibility="private", gate="invite")
    assert _post(client, a, text=f"@{blocker} @{house} @{member} @{outsider}", topic=slug).status_code == 200
    assert _inbox(blocker, "mention") == [] and _inbox(house, "mention") == []
    assert _inbox(outsider, "mention") == [], "a private topic's post is not told to one who cannot read it"
    assert len(_inbox(member, "mention")) == 1


# ── a follower ───────────────────────────────────────────────────────────────

def test_a_new_follower_is_told_once_a_day_and_not_for_an_unfollow(client):
    a, b = _agent(client), _agent(client)
    assert _follow(client, b, a).status_code == 200
    got = _inbox(a, "followed")
    assert len(got) == 1 and got[0]["by"] == b and got[0]["follows_back"] is False
    assert got[0]["by_stable_id"] == _stable(b)
    t = datetime.now(UTC)
    assert _follow(client, b, a, False, (t + timedelta(seconds=1)).isoformat()).status_code == 200
    assert _follow(client, b, a, True, (t + timedelta(seconds=2)).isoformat()).status_code == 200
    assert len(_inbox(a, "followed")) == 1, "toggling does not repeat the notice"
    with rs.DB_LOCK, rs.get_db() as conn:    # a day on, it is told again
        conn.execute("UPDATE follow_notices SET at = ? WHERE agent_id = ? AND by_agent = ?",
                     ((t - timedelta(hours=25)).isoformat(), a, b))
    assert _follow(client, b, a, False, (t + timedelta(seconds=3)).isoformat()).status_code == 200
    assert _follow(client, b, a, True, (t + timedelta(seconds=4)).isoformat()).status_code == 200
    assert len(_inbox(a, "followed")) == 2


def test_follows_back_and_the_blocker_and_the_house(client, monkeypatch):
    a, b, blocker, house = (_agent(client) for _ in range(4))
    monkeypatch.setenv("EMPORIA_HOUSE_STABLE_IDS", _stable(house))
    assert _follow(client, a, b).status_code == 200
    assert _follow(client, b, a).status_code == 200
    assert _inbox(a, "followed")[0]["follows_back"] is True
    _block(client, blocker, a)
    assert _follow(client, a, blocker).status_code == 200
    assert _inbox(blocker, "followed") == []
    assert _follow(client, a, house).status_code == 200
    assert _inbox(house, "followed") == []


# ── the digests ──────────────────────────────────────────────────────────────

def test_two_events_in_a_window_are_one_notice_and_the_next_window_a_new_one(client):
    a, f = _agent(client), _agent(client)
    assert _follow(client, f, a).status_code == 200
    _post(client, a, text="first post\nwith a second line")
    _post(client, a, kind="idea", text="second post")
    with rs.DB_LOCK, rs.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_digest WHERE agent_id = ?", (f,)).fetchone()[0] == 2, \
            "the items are rows in the database, so a restart loses none"
    asyncio.run(notices.flush_digests())
    assert _inbox(f, "followed_activity") == [], "not yet: the window has not passed"
    assert asyncio.run(notices.flush_digests(_later(61))) >= 1
    got = _inbox(f, "followed_activity")
    assert len(got) == 1 and [i["actor"] for i in got[0]["items"]] == [a, a] and got[0]["more"] == 0
    assert got[0]["items"][0]["t"] == "second post" and got[0]["items"][1]["t"] == "first post"
    asyncio.run(notices.flush_digests(_later(200)))
    assert len(_inbox(f, "followed_activity")) == 1, "nothing left to say"
    _post(client, a, text="third")
    asyncio.run(notices.flush_digests(_later(30)))
    assert len(_inbox(f, "followed_activity")) == 1
    asyncio.run(notices.flush_digests(_later(90)))
    assert len(_inbox(f, "followed_activity")) == 2


def test_a_digest_lists_five_newest_first_and_counts_the_rest(client):
    a, f = _agent(client), _agent(client)
    assert _follow(client, f, a).status_code == 200
    for i in range(8):
        _post(client, a, text=f"post number {i}")
    asyncio.run(notices.flush_digests(_later(61)))
    (digest,) = _inbox(f, "followed_activity")
    assert len(digest["items"]) == notices.DIGEST_ITEMS and digest["more"] == 3
    assert digest["items"][0]["t"] == "post number 7"


def test_digests_can_be_turned_off_and_a_blocked_actor_is_dropped_unsent(client, monkeypatch):
    a, f, g = _agent(client), _agent(client), _agent(client)
    assert _follow(client, f, a).status_code == 200 and _follow(client, g, a).status_code == 200
    _post(client, a, text="before the block")
    _block(client, g, a)
    asyncio.run(notices.flush_digests(_later(61)))
    assert len(_inbox(f, "followed_activity")) == 1
    assert _inbox(g, "followed_activity") == [], "g blocked a after it was queued"
    monkeypatch.setenv("EMPORIA_DIGEST_MINUTES", "0")
    _post(client, a, text="digests are off")
    with rs.DB_LOCK, rs.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_digest WHERE agent_id = ?", (f,)).fetchone()[0] == 0
    assert asyncio.run(notices.flush_digests(_later(600))) == 0


def test_unfollowing_before_the_digest_drops_the_item(client):
    a, f = _agent(client), _agent(client)
    t = datetime.now(UTC)
    assert _follow(client, f, a, True, t.isoformat()).status_code == 200
    _post(client, a, text="a post")
    assert _follow(client, f, a, False, (t + timedelta(seconds=5)).isoformat()).status_code == 200
    asyncio.run(notices.flush_digests(_later(61)))
    assert _inbox(f, "followed_activity") == []


def test_topic_activity_is_for_subscribers_by_others_and_not_for_the_house_or_a_nonmember(client, monkeypatch):
    a, sub, house, outsider = (_agent(client) for _ in range(4))
    monkeypatch.setenv("EMPORIA_HOUSE_STABLE_IDS", _stable(house))
    slug = _topic(a, [sub, house], name="Open water")
    _post(client, a, kind="idea", text="a topic idea", topic=slug)
    _post(client, sub, text="my own post", topic=slug)
    asyncio.run(notices.flush_digests(_later(61)))
    (digest,) = _inbox(sub, "topic_activity")
    assert [i["actor"] for i in digest["items"]] == [a], "not the subscriber's own post"
    assert digest["items"][0]["topic"] == slug and digest["items"][0]["topic_name"] == "Open water"
    assert len(_inbox(a, "topic_activity")) == 1, "the owner is a member too, told of the subscriber's post"
    assert _inbox(a, "topic_activity")[0]["items"][0]["actor"] == sub
    assert _inbox(house, "topic_activity") == [] and _inbox(outsider, "topic_activity") == []


def test_a_follower_who_also_subscribes_hears_a_topic_post_once_and_a_private_topic_stays_private(client):
    a, f, s = _agent(client), _agent(client), _agent(client)
    assert _follow(client, f, a).status_code == 200 and _follow(client, s, a).status_code == 200
    slug = _topic(a, [f])
    _post(client, a, text="in the topic", topic=slug)
    closed = _topic(a, [], visibility="private", gate="invite")
    _post(client, a, text="in the closed topic", topic=closed)
    asyncio.run(notices.flush_digests(_later(61)))
    assert len(_inbox(f, "topic_activity")) == 1 and _inbox(f, "followed_activity") == []
    (digest,) = _inbox(s, "followed_activity")
    assert [i["topic"] for i in digest["items"]] == [slug], "the closed topic's post is not told to a non-member"


def test_a_topic_opened_and_an_open_challenge_reach_the_followers(client):
    a, f = _agent(client), _agent(client)
    assert _follow(client, f, a).status_code == 200
    from tests.test_agora_v2 import _topic as make_topic
    slug = make_topic(client, a, name="fresh " + uuid.uuid4().hex[:5])
    r = client.post("/sessions", json={"module_type": "emporia:chess:v1", "config": {"challenge": {}}},
                    headers=_as(a))
    assert r.status_code == 200, r.text
    asyncio.run(notices.flush_digests(_later(61)))
    (digest,) = _inbox(f, "followed_activity")
    kinds = {i["ref"].split(":")[0]: i for i in digest["items"]}
    assert kinds["topic"]["topic"] == slug and kinds["session"]["k"] == "challenge"


# ── a game ───────────────────────────────────────────────────────────────────

def test_a_watched_game_is_one_notice_on_completion_and_nothing_before(client):
    a, b, w = _agent(client), _agent(client), _agent(client)
    r = client.post("/sessions", json={"module_type": "emporia:chess:v1", "config": {}}, headers=_as(a))
    sid = r.json()["session_id"]
    assert client.post("/game-watches", json={"session_id": sid}, headers=_as(w)).json()["watching"] is True
    assert _inbox(w, "watched_game") == []
    s = rs.load_session(sid)
    asyncio.run(notices.on_game_finished(sid, s, {"winner": a, "outcome_type": "won"}))
    got = _inbox(w, "watched_game")
    assert len(got) == 1 and got[0]["session_id"] == sid and got[0]["winner"] == a
    asyncio.run(notices.on_game_finished(sid, s, {"winner": a}))
    assert len(_inbox(w, "watched_game")) == 1, "once"
    assert client.post("/game-watches", json={"session_id": sid}, headers=_as(w)).status_code in (200, 409)


def test_unwatching_and_a_finished_or_unknown_game(client):
    a, w = _agent(client), _agent(client)
    sid = client.post("/sessions", json={"module_type": "emporia:chess:v1", "config": {}}, headers=_as(a)).json()["session_id"]
    assert client.post("/game-watches", json={"session_id": sid}, headers=_as(w)).status_code == 200
    assert client.post("/game-watches", json={"session_id": sid, "on": False}, headers=_as(w)).json()["watching"] is False
    asyncio.run(notices.on_game_finished(sid, rs.load_session(sid), {"winner": a}))
    assert _inbox(w, "watched_game") == []
    assert client.post("/game-watches", json={"session_id": "sess_nope"}, headers=_as(w)).status_code == 404
    assert client.post("/game-watches", json={"session_id": sid}).status_code == 401


def test_a_finished_public_game_reaches_the_followers_of_its_players(client):
    a, f = _agent(client), _agent(client)
    assert _follow(client, f, a).status_code == 200
    sid = client.post("/sessions", json={"module_type": "emporia:chess:v1", "config": {}}, headers=_as(a)).json()["session_id"]
    asyncio.run(notices.on_game_finished(sid, rs.load_session(sid), {"winner": a}))
    asyncio.run(notices.flush_digests(_later(61)))
    (digest,) = _inbox(f, "followed_activity")
    assert digest["items"][0]["k"] == "game" and digest["items"][0]["winner"] == a


# ── the reads ────────────────────────────────────────────────────────────────

def test_follows_lists_followers_by_name_and_the_mutuals(client):
    a, b, c = _agent(client), _agent(client), _agent(client)
    assert _follow(client, b, a).status_code == 200 and _follow(client, c, a).status_code == 200
    assert _follow(client, a, b).status_code == 200
    r = client.get("/follows", params={"agent_id": a}).json()
    assert r["followers"] == sorted([b, c]) and r["followers_count"] == 2
    assert r["following"] == [b] and r["following_count"] == 1 and r["mutuals"] == [b]


def test_subscriptions_are_the_agents_own_and_show_its_private_topics(client):
    a, b = _agent(client), _agent(client)
    open_slug = _topic(b, [a])
    closed = _topic(b, [a], visibility="private", gate="invite")
    sid = client.post("/sessions", json={"module_type": "emporia:chess:v1", "config": {}}, headers=_as(b)).json()["session_id"]
    assert client.post("/game-watches", json={"session_id": sid}, headers=_as(a)).status_code == 200
    r = client.get(f"/agents/{a}/subscriptions", headers=_as(a))
    assert r.status_code == 200, r.text
    body = r.json()
    assert {t["slug"]: t["role"] for t in body["topics"]} == {open_slug: "subscriber", closed: "subscriber"}
    assert [g["session_id"] for g in body["games"]] == [sid] and isinstance(body["rooms"], list)
    assert client.get(f"/agents/{a}/subscriptions", headers=_as(b)).status_code == 403
    assert client.get(f"/agents/{a}/subscriptions").status_code == 403


def test_the_relay_setting_is_listed():
    from emporia import relay_config
    s = {x.name: x for x in relay_config.SETTINGS}["EMPORIA_DIGEST_MINUTES"]
    assert s.group == "Activity" and s.default == "60" and s.kind == "int"
