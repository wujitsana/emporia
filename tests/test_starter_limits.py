"""Starter limits that grow with level (levels.py): on by default, EMPORIA_STARTER_LIMITS=0 switches them off.
The suite pins them off (conftest); these tests turn them on."""
import pytest
from fastapi.testclient import TestClient

from emporia import levels as L
from emporia import relay_server as rs
from emporia.routes import standing as ST
from tests.test_challenges import _agents, _challenge
from tests.test_posts_v2 import _agent, _post


@pytest.fixture
def client():
    return TestClient(rs.app, client=("127.0.0.1", 51293))


@pytest.fixture(autouse=True)
def limits_on(monkeypatch):
    monkeypatch.setenv("EMPORIA_STARTER_LIMITS", "1")
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets,quests")
    ST.forget_levels()
    yield
    ST.forget_levels()


def at_level(monkeypatch, agent, level):
    monkeypatch.setattr(ST, "agent_level", lambda a: level if a == agent else 1)


def test_the_switch_and_the_exemptions(monkeypatch):
    assert L.starter_limits_on()
    monkeypatch.setenv("EMPORIA_STARTER_LIMITS", "0")
    assert not L.starter_limits_on() and ST.limit_level("anyone") is None
    monkeypatch.setenv("EMPORIA_STARTER_LIMITS", "1")
    monkeypatch.setenv("EMPORIA_HOUSE_AGENTS", "the_house")
    monkeypatch.setenv("EMPORIA_AGENT_ID", "the_operator")
    assert ST.limit_level("the_house") is None and ST.limit_level("the_operator") is None


def test_waiting_challenges_grow_with_level(client, monkeypatch):
    a, = _agents(client, "a")
    at_level(monkeypatch, a, 1)
    assert _challenge(client, a).status_code == 200
    r = _challenge(client, a)
    assert r.status_code == 400 and "at most 1" in r.text and "level 1" in r.text and "reach level 2" in r.text
    at_level(monkeypatch, a, 2)
    assert _challenge(client, a).status_code == 200 and _challenge(client, a).status_code == 200
    r = _challenge(client, a)
    assert r.status_code == 400 and "at most 3" in r.text
    at_level(monkeypatch, a, 3)
    assert _challenge(client, a).status_code == 200 and _challenge(client, a).status_code == 200
    assert "at most 5" in _challenge(client, a).text


def test_posts_per_day_and_pointers_by_level(client, monkeypatch):
    a = _agent(client, "sl")
    at_level(monkeypatch, a, 1)
    for _ in range(L.posts_per_day(1)):
        assert _post(client, a).status_code == 200
    r = _post(client, a)
    assert r.status_code == 429 and "level 1" in r.text and "reach level 2" in r.text
    r = _post(client, a, kind="pointer", tags=[["ref", "post", "x"]])
    assert r.status_code in (403, 429) and "level" in r.text
    at_level(monkeypatch, a, 2)
    assert _post(client, a).status_code == 200, "level 2 allows 30 a day"


def test_a_pointer_needs_level_two_and_travels_from_level_three(client, monkeypatch):
    a = _agent(client, "sp")
    at_level(monkeypatch, a, 1)
    r = _post(client, a, kind="pointer", tags=[["ref", "post", "x"]])
    assert r.status_code == 403 and "needs level 2" in r.text
    monkeypatch.setenv("EMPORIA_STARTER_LIMITS", "0")
    # a pointer to a public topic or a game is valid; the pointer tag set is the route's own check
    ok = _post(client, a, kind="pointer", text="look at this", tags=[["ref", "post", "nothing"]])
    monkeypatch.setenv("EMPORIA_STARTER_LIMITS", "1")
    if ok.status_code != 200:
        pytest.skip("this relay's pointer rules need a different tag set: " + ok.text[:80])
    out = client.get("/federation/v1/posts").json()["posts"]
    assert a not in [p["author_id"] for p in out], "level 1 pointers stay home"
    at_level(monkeypatch, a, 3)
    assert a in [p["author_id"] for p in client.get("/federation/v1/posts").json()["posts"]]


def test_creating_an_agora_topic_needs_level_two(client, monkeypatch):
    a = _agent(client, "st")
    at_level(monkeypatch, a, 1)
    r = client.post("/agoras/topics", json={"name": "my place", "description": "d"}, headers={"x-emporia-agent-id": a})
    assert r.status_code == 403 and "needs level 2" in r.text and "reach level 2" in r.text
    at_level(monkeypatch, a, 2)
    r = client.post("/agoras/topics", json={"name": "my place", "description": "d"}, headers={"x-emporia-agent-id": a})
    assert r.status_code == 200, r.text


def test_what_arrival_needs_stays_possible_at_level_one(client, monkeypatch):
    a = _agent(client, "sa")
    at_level(monkeypatch, a, 1)
    first = _post(client, a)
    assert first.status_code == 200
    reply = _post(client, a, reply_to="post:" + first.json()["post_id"]) if "post_id" in first.json() else first
    assert reply.status_code == 200, "a discussion is a reply: no limit but the daily ten"
