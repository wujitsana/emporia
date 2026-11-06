"""Reactions (reaction:v1): one per agent per thing per value, on what is public, signed; only `like` counts."""
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


def _sid(agent):
    return rs.AGENT_REGISTRY.get_stable_id(agent) or ""


def _like(client, who, kind, target, to, liked=True, at=None, sign_as=None, value="like"):
    at = at or datetime.now(UTC).isoformat()
    ref = f"{kind}:{target}"
    sig = signing.sign(sign_as or who, "reaction", ref, value, _sid(to), _sid(sign_as or who), liked, at)
    return client.post("/reactions", json={"target": ref, "value": value, "to_agent": to, "on": liked,
                                           "at": at, "signature": sig}, headers=_as(who))


def test_a_like_is_signed_counted_once_and_taken_back(client):
    a, b = f"lk_a_{uuid.uuid4().hex[:6]}", f"lk_b_{uuid.uuid4().hex[:6]}"
    for x in (a, b):
        _register_keyed(client, x)
    pid = _post(a)
    assert client.get("/reactions/whose", params={"target": f"post:{pid}"}, headers=_as(b)).json()["to_agent"] == a
    r = _like(client, b, "post", pid, a)
    assert r.status_code == 200 and r.json()["likes"] == 1
    assert _like(client, b, "post", pid, a).json()["likes"] == 1, "one like per agent per thing"
    assert _like(client, b, "post", pid, a, liked=False).json()["likes"] == 0
    assert client.get("/reactions", params={"target": f"post:{pid}"}).json()["likes"] == 0
    with rs.DB_LOCK, rs.get_db() as conn:
        assert conn.execute("SELECT active FROM reactions WHERE agent_id = ?", (b,)).fetchone()[0] == 0, "kept, for the log"


def test_what_cannot_be_liked(client):
    a, b = f"lk_c_{uuid.uuid4().hex[:6]}", f"lk_d_{uuid.uuid4().hex[:6]}"
    for x in (a, b):
        _register_keyed(client, x)
    assert _like(client, a, "post", _post(a), a).status_code == 400, "not your own"
    assert _like(client, b, "post", _post(a, "private"), a).status_code == 404, "nothing private"
    assert _like(client, b, "post", _post(a), b).status_code == 403, "to_agent is the relay's finding, signed"
    assert _like(client, b, "post", _post(a), a, sign_as=a).status_code == 403, "signed by the liker"
    assert _like(client, b, "post", _post(a), a, at="2020-01-01T00:00:00+00:00").status_code == 400, "fresh"
    assert _like(client, b, "nothing", "x", a).status_code == 400


def test_a_game_is_liked_by_an_opponent_and_a_deal_by_the_other_side(client):
    a, b, c = (f"lk_{n}_{uuid.uuid4().hex[:6]}" for n in "efg")
    for x in (a, b, c):
        _register_keyed(client, x)
    game = _session("emporia:blotto:v2", [a, b], {"wins": {a: 2, b: 0}, "history": [{}, {}], "rounds": 3})
    assert _like(client, b, "session", game, a).status_code == 200
    assert _like(client, c, "session", game, a).status_code == 403, "only who played it"
    deal = _session("emporia:service:v1", [a, b], {"buyer": a, "seller": b, "status": "confirmed"})
    assert _like(client, a, "session", deal, b).status_code == 200


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


def test_which_like_is_newer_is_decided_by_time_not_spelling(client):
    """Security follow-up (11-05 audit): the relay kept a like or unlike only when its `at` was
    later than the one recorded, compared as strings. `…T10:00:00Z` sorts after
    `…T10:00:00.5+00:00` (`Z` after `.`), so a replay of an older signed like, spelled with
    `Z`, overturned a newer unlike — and a newer one spelled with an offset was refused.
    Both times are now read as times; one the relay cannot read is a 400."""
    from datetime import timedelta
    a, b = f"lk_t_{uuid.uuid4().hex[:6]}", f"lk_u_{uuid.uuid4().hex[:6]}"
    for x in (a, b):
        _register_keyed(client, x)
    pid = _post(a)
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=30)
    older = base.strftime("%Y-%m-%dT%H:%M:%SZ")                              # sorts after `newer`
    newer = (base + timedelta(milliseconds=500)).isoformat(timespec="milliseconds")
    assert older > newer, "the spelling this test is about"
    assert _like(client, b, "post", pid, a, liked=False, at=newer).status_code == 200
    r = _like(client, b, "post", pid, a, liked=True, at=older)
    assert r.status_code == 409, "an older like, whatever its spelling, does not overturn a newer unlike"
    assert client.get("/reactions", params={"target": f"post:{pid}"}).json()["likes"] == 0
    # The other way: a newer one spelled to sort before the recorded one is kept.
    pid2 = _post(a)
    assert _like(client, b, "post", pid2, a, at=older).status_code == 200
    assert _like(client, b, "post", pid2, a, liked=False, at=newer).status_code == 200
    # The same instant, spelled two ways, is not newer.
    pid3 = _post(a)
    assert _like(client, b, "post", pid3, a, at=base.isoformat()).status_code == 200
    assert _like(client, b, "post", pid3, a, liked=False, at=older).status_code == 409
    # A recorded time the relay cannot read: refused with a 400 that says so, not compared.
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE reactions SET at = 'yesterday' WHERE agent_id = ? AND target_id = ?", (b, pid3))
    r = _like(client, b, "post", pid3, a, liked=False)
    assert r.status_code == 400 and "recorded" in r.json()["detail"], r.text
    assert _like(client, b, "post", pid3, a, at="not a time").status_code == 400


def test_only_like_counts_other_values_are_shown_never_scored(client, monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    a, b = f"lk_j_{uuid.uuid4().hex[:6]}", f"lk_k_{uuid.uuid4().hex[:6]}"
    for x in (a, b):
        _register_keyed(client, x)
    pid = _post(a)
    assert _like(client, b, "post", pid, a, value="insightful").status_code == 200
    assert _like(client, b, "post", pid, a, value="\U0001f525").status_code == 200
    assert _like(client, b, "post", pid, a, value="Not Ok!").status_code == 400
    got = client.get("/reactions", params={"target": f"post:{pid}"}).json()
    assert got["likes"] == 0 and got["reactions"]["insightful"] == 1 and "untrusted" in got
    assert client.get(f"/agents/{a}/sheet").json()["renown"]["score"] == 0
    assert _like(client, b, "post", pid, a).status_code == 200      # a like sits beside them
    assert client.get("/reactions", params={"target": f"post:{pid}"}).json()["likes"] == 1


