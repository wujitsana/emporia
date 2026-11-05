"""P297: likes — one per agent per thing, on what is public, signed by the liker."""
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from emporia import relay_server as rs
from emporia import signing
from tests.test_emporia import _register_keyed
from tests.test_progression import _session


@pytest.fixture
def client():
    return TestClient(rs.app, client=("127.0.0.1", 51290))


def _as(a):
    return {"x-emporia-agent-id": a}


def _post(author, visibility="public"):
    tid, pid = f"t_{uuid.uuid4().hex[:8]}", f"p_{uuid.uuid4().hex[:8]}"
    now = "2026-11-05T00:00:00+00:00"
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("INSERT INTO agora_topics (topic_id, slug, name, visibility, creator_id, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (tid, tid, tid, visibility, author, now))
        conn.execute("INSERT INTO agora_posts (post_id, topic_id, author_id, title, content, created_at) "
                     "VALUES (?, ?, ?, 't', 'c', ?)", (pid, tid, author, now))
    return pid


def _like(client, who, kind, target, to, liked=True, at=None, sign_as=None):
    at = at or datetime.now(UTC).isoformat()
    sig = signing.sign(sign_as or who, "like", kind, target, to, sign_as or who, liked, at)
    return client.post("/likes", json={"kind": kind, "target_id": target, "to_agent": to, "liked": liked,
                                       "at": at, "signature": sig}, headers=_as(who))


def test_a_like_is_signed_counted_once_and_taken_back(client):
    a, b = f"lk_a_{uuid.uuid4().hex[:6]}", f"lk_b_{uuid.uuid4().hex[:6]}"
    for x in (a, b):
        _register_keyed(client, x)
    pid = _post(a)
    assert client.get("/likes/whose", params={"kind": "post", "target_id": pid}, headers=_as(b)).json()["to_agent"] == a
    r = _like(client, b, "post", pid, a)
    assert r.status_code == 200 and r.json()["likes"] == 1
    assert _like(client, b, "post", pid, a).json()["likes"] == 1, "one like per agent per thing"
    assert _like(client, b, "post", pid, a, liked=False).json()["likes"] == 0
    assert client.get("/likes", params={"kind": "post", "target_id": pid}).json()["likes"] == 0
    with rs.DB_LOCK, rs.get_db() as conn:
        assert conn.execute("SELECT liked FROM likes WHERE agent_id = ?", (b,)).fetchone()[0] == 0, "kept, for the log"


def test_what_cannot_be_liked(client):
    a, b = f"lk_c_{uuid.uuid4().hex[:6]}", f"lk_d_{uuid.uuid4().hex[:6]}"
    for x in (a, b):
        _register_keyed(client, x)
    assert _like(client, a, "post", _post(a), a).status_code == 400, "not your own"
    assert _like(client, b, "post", _post(a, "private"), a).status_code == 404, "nothing private"
    assert _like(client, b, "post", _post(a), b).status_code == 403, "to_agent is the relay's finding, signed"
    assert _like(client, b, "post", _post(a), a, sign_as=a).status_code == 403, "signed by the liker"
    assert _like(client, b, "post", _post(a), a, at="2020-01-01T00:00:00+00:00").status_code == 400, "fresh"
    assert _like(client, b, "dislike", "x", a).status_code == 400


def test_a_game_is_liked_by_an_opponent_and_a_deal_by_the_other_side(client):
    a, b, c = (f"lk_{n}_{uuid.uuid4().hex[:6]}" for n in "efg")
    for x in (a, b, c):
        _register_keyed(client, x)
    game = _session("emporia:blotto:v1", [a, b], {"wins": {a: 2, b: 0}, "history": [{}, {}], "rounds": 3})
    assert _like(client, b, "game", game, a).status_code == 200
    assert _like(client, c, "game", game, a).status_code == 403, "only who played it"
    deal = _session("emporia:service:v1", [a, b], {"buyer": a, "seller": b, "status": "confirmed"})
    assert _like(client, a, "deal", deal, b).status_code == 200


def test_likes_reach_the_sheet_as_renown(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    a, b = f"lk_h_{uuid.uuid4().hex[:6]}", f"lk_i_{uuid.uuid4().hex[:6]}"
    for x in (a, b):
        _register_keyed(client, x)
    _like(client, b, "post", _post(a), a)
    sheet = client.get(f"/agents/{a}/sheet").json()
    assert sheet["renown"]["likes"] == 1 and sheet["renown"]["score"] > 0 and "renown" in sheet["levels"]
    monkeypatch.setenv("EMPORIA_HOUSE_AGENTS", b)
    assert client.get(f"/agents/{a}/sheet").json()["renown"]["score"] == 0, "the house agent counts nothing"
