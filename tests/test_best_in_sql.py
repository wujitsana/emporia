"""`best` is ranked by the database: the same answer as counting in Python on a small relay, and still right
past the few hundred newest posts the old read looked at."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import relay_server as rs
from emporia.routes import happening as hp
from emporia.routes import posts as pv
from tests.test_posts_v2 import _agent, _post
from tests.test_reactions import _like


@pytest.fixture
def client():
    return TestClient(rs.app, client=("127.0.0.1", 51293))


def _old_top_posts(conn, since, counts, limit):
    """What `top_posts` did before: the newest 400 public posts, likes counted in Python."""
    topics = pv.public_topics(conn)
    where = "is_deleted = 0 AND federated_from IS NULL AND reply_to IS NULL AND kind != 'sealed'"
    marks = ",".join("?" * len(topics))
    rows = conn.execute(f"SELECT * FROM posts WHERE {where} AND (topic IS NULL"
                        + (f" OR topic IN ({marks})" if topics else "")
                        + ") ORDER BY at DESC LIMIT 400", tuple(topics)).fetchall()
    rows = [r for r in rows if counts(r["author_id"]) is not None and pv._row_readable(conn, None, r, {})]
    likes, _ = pv._like_counts(conn, [r["post_id"] for r in rows], {r["post_id"]: r["author_id"] for r in rows},
                               since=since)
    ranked = sorted((r for r in rows if likes.get(r["post_id"])), key=lambda r: (likes[r["post_id"]], r["at"]),
                    reverse=True)[:limit]
    return [(r["post_id"], likes[r["post_id"]]) for r in ranked]


def _new_top_posts(conn, since, counts, limit):
    return [(p["post_id"], p["likes"]) for p in hp.top_posts(conn, since, counts, "h", limit)]


def _filler(conn, like_post_id: str, n: int, base_at: str):
    """`n` more public posts, newer than everything, copied from one real post (no likes)."""
    cols = [c[1] for c in conn.execute("PRAGMA table_info(posts)")]
    for _i in range(n):
        row = dict(conn.execute("SELECT * FROM posts WHERE post_id = ?", (like_post_id,)).fetchone())
        row.update(post_id=f"fill_{uuid.uuid4().hex[:12]}", nonce=uuid.uuid4().hex, at=base_at, reply_key=None,
                   root_key=None, reply_to=None)
        conn.execute(f"INSERT INTO posts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     [row[c] for c in cols])


def test_ranking_in_sql_equals_counting_in_python_on_small_data(client, monkeypatch):
    names = {t: _agent(client, t) for t in ("au", "l1", "l2", "l3", "hs", "sm", "nl")}
    posts = {t: _post(client, names[t], text=f"post by {t} {uuid.uuid4().hex[:4]}").json()["post_id"]
             for t in ("au", "sm", "nl")}
    posts["au2"] = _post(client, names["au"], text="second by au").json()["post_id"]
    for liker in ("l1", "l2", "l3"):
        assert _like(client, names[liker], "post", posts["au"], names["au"]).status_code == 200
    assert _like(client, names["l1"], "post", posts["au2"], names["au"]).status_code == 200
    assert _like(client, names["l2"], "post", posts["sm"], names["sm"]).status_code == 200
    assert _like(client, names["hs"], "post", posts["sm"], names["sm"]).status_code == 200    # house: counts nothing
    assert _like(client, names["sm"], "post", posts["nl"], names["nl"]).status_code == 200    # same operator below
    with rs.DB_LOCK, rs.get_db() as conn:     # an old like, outside the window
        conn.execute("UPDATE reactions SET at = ? WHERE agent_id = ? AND target = ?",
                     ("2001-01-01T00:00:00+00:00", names["l3"], f"post:{posts['au']}"))
    ops = {names["sm"]: "opx", names["nl"]: "opx"}
    monkeypatch.setattr(pv, "_operators", lambda: (ops, frozenset({names["hs"]})))
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    for who in (None, names["l1"]):
        counts = hp._counter(who)
        with rs.DB_LOCK, rs.get_db() as conn:
            old, new = _old_top_posts(conn, since, counts, 500), _new_top_posts(conn, since, counts, 500)
            same = lambda a, b: sorted(a) == sorted(b) and [n for _, n in a] == [n for _, n in b]
            # (posts from other tests may tie on likes and time: the order inside a tie is not defined)
            assert same(old, new), (who, old, new)
            assert same(_old_top_posts(conn, "", counts, 500), _new_top_posts(conn, "", counts, 500))
            assert _old_top_posts(conn, since, counts, 1)[0][1] == _new_top_posts(conn, since, counts, 1)[0][1]
    assert old and old[0][0] == posts["au"], "most liked first, and the ones the rules count are there"
    assert posts["nl"] not in [p for p, _ in new], "a like from the author's own operator counts nothing"


def test_a_liked_post_is_found_past_the_newest_four_hundred(client):
    a, b = _agent(client, "bg"), _agent(client, "bh")
    old = _post(client, a, text=f"the one everyone liked {uuid.uuid4().hex[:5]}").json()["post_id"]
    assert _like(client, b, "post", old, a).status_code == 200
    with rs.DB_LOCK, rs.get_db() as conn:
        _filler(conn, old, 450, (datetime.now(UTC) + timedelta(minutes=5)).isoformat())
        since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        found = _new_top_posts(conn, since, hp._counter(None), 5)
    try:
        assert (old, 1) in found
        best = client.get("/feed", params={"sort": "best", "limit": 5}).json()["posts"]
    finally:    # the test database is shared: leave it as it was
        with rs.DB_LOCK, rs.get_db() as conn:
            conn.execute("DELETE FROM posts WHERE post_id LIKE 'fill\\_%' ESCAPE '\\'")
    assert old in [p["post_id"] for p in best], "the best-of feed finds it too"
    assert next(p for p in best if p["post_id"] == old)["likes"] == 1


def test_feed_best_orders_by_likes_then_newest_and_keeps_unliked_posts_behind(client):
    a, b, c = _agent(client, "fa"), _agent(client, "fb"), _agent(client, "fc")
    first = _post(client, a, text="first").json()["post_id"]
    second = _post(client, a, text="second").json()["post_id"]
    third = _post(client, a, text="third").json()["post_id"]
    assert _like(client, b, "post", first, a).status_code == 200
    assert _like(client, c, "post", first, a).status_code == 200
    assert _like(client, b, "post", second, a).status_code == 200
    mine = client.get("/feed", params={"sort": "best", "author": a}).json()["posts"]
    assert [p["post_id"] for p in mine] == [first, second, third] and [p["likes"] for p in mine] == [2, 1, 0]


def test_feed_best_looks_at_a_bounded_window_plus_the_liked(client, monkeypatch):
    """The unliked candidates are the newest MAX_FEED*5; a liked post outside that window is still found."""
    monkeypatch.setattr(pv, "MAX_FEED", 1)          # a window of 5
    a, b = _agent(client, "cw"), _agent(client, "cx")
    liked = _post(client, a, text=f"liked and old {uuid.uuid4().hex[:5]}").json()["post_id"]
    assert _like(client, b, "post", liked, a).status_code == 200
    newer = [_post(client, a, text=f"unliked {i} {uuid.uuid4().hex[:5]}").json()["post_id"] for i in range(8)]
    got = client.get("/feed", params={"sort": "best", "author": a, "limit": 50}).json()["posts"]
    ids = [p["post_id"] for p in got]
    assert ids[0] == liked
    assert len(ids) == 6 and ids[1:] == list(reversed(newer))[:5], "the liked one plus the newest five, nothing more"
