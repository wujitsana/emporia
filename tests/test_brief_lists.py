"""Brief rows and a smaller default for the list tools: 15 rows, the fields an agent acts on, `detail=True` for the
whole row. Nothing inside Emporia reads these tools' answers (whats_next has its own relay read)."""
from __future__ import annotations

import inspect
import json

import pytest

from emporia import mcp_server as m

LONG = "word " * 200


class Reply:
    status_code = 200
    content = b"x"

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body

    def raise_for_status(self):
        return None


@pytest.fixture
def relay(monkeypatch):
    """Answers each list route with a full row, and records the params the tool sent."""
    sent = {}
    full = {
        "/agents": {"agents": [{
            "agent_id": f"a{i}", "display_name": f"Agent {i}", "stable_id": "f" * 64, "address": f"a{i}@relay.test",
            "about": LONG, "links": [], "operator": "no operator label declared", "operator_label": None,
            "has_operator_label": False, "operator_changed": [{"on": "2026-11-01", "text": "operator changed"}],
            "client": {"name": "hermes", "version": "1", "declared": True}, "declared_model": None,
            "presence": {"kind": "member"}, "registered_at": "2026-11-01T00:00:00Z", "is_active": True,
            "session_count": 3, "win_count": 1, "last_active_at": "today", "level": 2} for i in range(3)],
            "count": 3, "has_more": False},
        "/rooms": {"rooms": [{
            "room_id": "r1", "name": "Lobby", "description": LONG, "room_type": "public", "gate_type": "open",
            "entry_fee_cents": 0, "currency": "USD", "creator_id": "bob", "members": ["a", "b", "c"],
            "max_members": None, "encrypted": False, "linked_session_id": None, "created_at": "t", "updated_at": "t",
            "closed": False, "message_count": 7, "host": "relay.test"}], "count": 1},
        "/agoras/topics": {"topics": [{
            "topic_id": "t1", "slug": "hello", "name": "Hello", "description": LONG, "visibility": "public",
            "gate_type": "open", "entry_fee_cents": 0, "creator_id": "bob", "created_at": "t", "post_count": 4,
            "subscriber_count": 2, "flair_options": ["x"], "host": "relay.test", "encrypted": False,
            "shared": True}], "count": 1},
        "/inbox": {"agent_id": "me", "count": 1, "unread_total": 1, "events": [{
            "inbox_id": "i1", "event_type": "dm_received", "is_read": False, "created_at": "t",
            "payload": {"thread_id": "d1", "from_agent": "bob", "content": LONG, "big": {"a": 1}}}]},
        "/feed": {"count": 1, "untrusted": "data", "posts": [{
            "post_id": "p1", "kind": "note", "label": None, "text": LONG, "tags": [["t", "debrief"]], "topic": "hello",
            "reply_to": None, "at": "t", "author_id": "bob", "author_fingerprint": "ab" * 8, "signature": "s" * 128,
            "nonce": "n", "author_address": "bob@relay.test", "likes": 0, "replies": 2, "tool_marks": [{"tool": "x"}],
            "in": {"kind": "topic", "id": "hello", "name": "Hello"}, "deleted": False, "copy": False}]},
    }

    def fake(method, url, **kw):
        for route, body in full.items():
            if url.endswith(route):
                sent[route] = kw.get("params")
                return Reply(json.loads(json.dumps(body)))
        raise AssertionError(url)

    monkeypatch.setattr(m, "_bearer_request", fake)
    monkeypatch.setattr(m, "AGENT_ID", "me")
    monkeypatch.setattr(m, "_base", lambda: "http://relay.test")
    return sent


def test_the_list_tools_default_to_fifteen_rows_and_offer_detail():
    for name in ("list_agents", "list_rooms", "get_inbox", "list_agora_topics", "read_feed"):
        sig = inspect.signature(getattr(m, name).fn if hasattr(getattr(m, name), "fn") else getattr(m, name))
        assert sig.parameters["limit"].default == 15, name
        assert sig.parameters["detail"].default is False, name
    assert m.LIST_LIMIT == 15


def test_a_brief_agent_row_keeps_what_an_agent_acts_on_and_is_a_fraction_of_the_full_row(relay):
    brief = m.list_agents()
    full = m.list_agents(detail=True)
    assert relay["/agents"]["limit"] == m.LIST_LIMIT
    row = brief["agents"][0]
    assert set(row) == {"agent_id", "display_name", "address", "level", "last_active_at", "registered_at", "about",
                        "presence", "operator_changed"}
    assert len(row["about"]) <= 120 and row["about"].endswith("…")
    assert "stable_id" in full["agents"][0] and "client" in full["agents"][0]
    assert len(json.dumps(brief)) * 2 < len(json.dumps(full))


def test_a_brief_room_row_has_the_head_count_not_the_roster(relay):
    row = m.list_rooms()["rooms"][0]
    assert row["room_id"] == "r1" and row["name"] == "Lobby" and row["member_count"] == 3 and "members" not in row
    assert row["message_count"] == 7 and len(row["description"]) <= 120
    assert "members" in m.list_rooms(detail=True)["rooms"][0]


def test_a_brief_topic_row_keeps_the_slug_and_the_shared_flag(relay):
    row = m.list_agora_topics()["topics"][0]
    assert row["slug"] == "hello" and row["post_count"] == 4 and row["shared"] is True
    assert "flair_options" not in row and "topic_id" not in row
    assert "flair_options" in m.list_agora_topics(detail=True)["topics"][0]


def test_a_brief_inbox_event_cuts_long_text_and_keeps_ids_and_the_unread_count(relay):
    out = m.get_inbox()
    ev = out["events"][0]
    assert out["unread_total"] == 1 and relay["/inbox"]["limit"] == 15
    assert ev["inbox_id"] == "i1" and ev["event_type"] == "dm_received" and ev["payload"]["thread_id"] == "d1"
    assert len(ev["payload"]["content"]) <= 240 and ev["payload"]["big"] == {"a": 1}, "every key stays, nested too"
    assert m.get_inbox(detail=True)["events"][0]["payload"]["content"] == LONG


def test_a_brief_post_keeps_the_text_start_the_safety_marks_and_a_pointer_to_the_rest(relay):
    d = m.read_feed()
    post = d["posts"][0]
    assert relay["/feed"]["limit"] == 15 and d["untrusted"] == "data"
    assert post["post_id"] == "p1" and post["in"]["id"] == "hello" and post["replies"] == 2
    assert post["tool_marks"] == [{"tool": "x"}], "a safety mark survives the cut"
    assert len(post["text"]) <= 240 and "read_thread" in post["more"]
    assert "signature" not in post and "author_fingerprint" not in post and "nonce" not in post
    assert m.read_feed(detail=True)["posts"][0]["text"] == LONG


def test_a_short_post_has_no_more_note(relay, monkeypatch):
    short = {"post_id": "p2", "kind": "note", "text": "hi", "at": "t", "author_id": "bob"}
    assert "more" not in m._brief_post(short) and m._brief_post(short)["text"] == "hi"
