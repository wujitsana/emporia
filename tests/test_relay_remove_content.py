"""`emporia relay remove NAME --delete-content`: the operator's removal also erases what the agent posted,
the way a moderator's removal does; without the flag the content stays."""
import argparse
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from emporia import cli, deletion
from emporia import relay_server as rs
from emporia.relay_server import app
from tests.test_deletion import _room_message
from tests.test_emporia import _register_keyed, _signed_room
from tests.topic_posts import v2_post


def _h(a):
    return {"x-emporia-agent-id": a}


def _db(sql, *args):
    with rs.DB_LOCK, rs.get_db() as conn:
        return conn.execute(sql, args).fetchall()


@pytest.fixture
def world(monkeypatch):
    """A spam agent and a bystander sharing a topic, a room and a game; everyone has posted."""
    client = TestClient(app, client=("127.0.0.1", 51910))
    tag = uuid.uuid4().hex[:6]
    spam, ok = f"spam_{tag}", f"ok_{tag}"
    for a in (spam, ok):
        _register_keyed(client, a)
    slug = client.post("/agoras/topics", json={"name": f"T {tag}", "description": "d"}, headers=_h(ok)).json()["slug"]
    client.post(f"/agoras/topics/{slug}/subscribe", json={}, headers=_h(spam))
    spam_post = v2_post(client, spam, slug, "buy pills now").json()["post_id"]
    ok_post = v2_post(client, ok, slug, "a real thought").json()["post_id"]
    spam_reply = v2_post(client, spam, slug, "pills, replying", reply_to=f"post:{ok_post}").json()["post_id"]
    room = client.post("/rooms", json=_signed_room(ok, {"name": f"R {tag}"}), headers=_h(ok)).json()["room_id"]
    client.post(f"/rooms/{room}/join", json={"agent_id": spam}, headers=_h(spam))
    spam_msg = _room_message(client, room, spam, "pills in the room")
    ok_msg = _room_message(client, room, ok, "hello room")
    sid = client.post("/sessions", json={"module_type": "emporia:chess:v1", "creator_agent_id": ok},
                      headers=_h(ok)).json()["session_id"]
    now = datetime.now(UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        for mid, who, text in ((f"gc_s_{tag}", spam, "pills in chat"), (f"gc_o_{tag}", ok, "gg")):
            conn.execute("INSERT INTO game_chat (message_id, session_id, agent_id, content, created_at) "
                         "VALUES (?,?,?,?,?)", (mid, sid, who, text, now))
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    answers: list[str] = []

    def _prompt(q, default=""):
        answers.append(q)
        return spam
    monkeypatch.setattr(cli, "prompt", _prompt)
    return dict(client=client, spam=spam, ok=ok, slug=slug, room=room, sid=sid, tag=tag, answers=answers,
                spam_posts=[spam_post, spam_reply], ok_post=ok_post, spam_msg=spam_msg, ok_msg=ok_msg)


def _remove(name, flag):
    return cli.cmd_relay_remove(argparse.Namespace(url=name, delete_content=flag))


def test_delete_content_erases_posts_messages_and_game_chat(world):
    w, c = world, world["client"]
    assert _remove(w["spam"], True) == 0
    assert "delete its 2 posts, 1 room messages and 1 game chat" in w["answers"][0]
    for p in w["spam_posts"]:
        assert c.get(f"/agoras/posts/{p}", headers=_h(w["ok"])).status_code == 404
        row = _db("SELECT text, is_deleted FROM posts WHERE post_id = ?", p)[0]
        assert (row["text"], row["is_deleted"]) == ("", 1)
        with rs.DB_LOCK, rs.get_db() as conn:
            rec = deletion.removal_of(conn, "post", p)
        assert rec["role"] == "operator" and rec["by"] == "operator"
    topic = c.get(f"/agoras/topics/{w['slug']}/posts", headers=_h(w["ok"])).json()
    assert "pills" not in str(topic)
    assert "a real thought" in str(topic)
    msgs = c.get(f"/rooms/{w['room']}/messages", headers=_h(w["ok"])).json()["messages"]
    gone = {m["message_id"]: m for m in msgs if m.get("deleted_at")}
    assert set(gone) == {w["spam_msg"]} and gone[w["spam_msg"]]["content"] == ""
    assert [m for m in msgs if m["message_id"] == w["ok_msg"]][0]["content"] != ""
    chat = {r["agent_id"]: r for r in _db("SELECT * FROM game_chat WHERE session_id = ?", w["sid"])}
    assert chat[w["spam"]]["content"] == "" and chat[w["spam"]]["deleted_at"]
    assert chat[w["ok"]]["content"] == "gg" and chat[w["ok"]]["deleted_at"] is None
    # untouched: the bystander's post, the session, the topic and room themselves
    assert c.get(f"/agoras/posts/{w['ok_post']}", headers=_h(w["ok"])).status_code == 200
    assert _db("SELECT status FROM sessions WHERE session_id = ?", w["sid"])[0]["status"] == "waiting"
    assert _db("SELECT is_deleted FROM agora_topics WHERE slug = ?", w["slug"])[0]["is_deleted"] == 0
    assert _db("SELECT is_active FROM authorized_agents WHERE agent_id = ?", w["spam"])[0]["is_active"] == 0


def test_without_the_flag_content_stays(world):
    w = world
    assert _remove(w["spam"], False) == 0
    assert "delete its" not in w["answers"][0]
    for p in w["spam_posts"]:
        assert w["client"].get(f"/agoras/posts/{p}", headers=_h(w["ok"])).status_code == 200
    assert not _db("SELECT 1 FROM deletions WHERE item IN (?, ?)", *w["spam_posts"])
    assert _db("SELECT content FROM game_chat WHERE agent_id = ?", w["spam"])[0]["content"] == "pills in chat"


def test_an_agent_that_already_left_is_removed_and_its_content_deleted(world):
    from emporia.routes import agents as agent_routes
    w = world
    agent_routes.retire(w["spam"])                  # its own leave
    assert _remove(w["spam"], True) == 0
    assert "already left" in w["answers"][0] and "delete its 2 posts" in w["answers"][0]
    for p in w["spam_posts"]:
        assert w["client"].get(f"/agoras/posts/{p}", headers=_h(w["ok"])).status_code == 404
    assert _db("SELECT content FROM game_chat WHERE agent_id = ?", w["spam"])[0]["content"] == ""
