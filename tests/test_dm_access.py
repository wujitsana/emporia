"""EMPORIA_DM_AGENT_ACCESS: how much private message text the agent's own tools may return."""
import json

import pytest

from emporia import crypto_box, dm_access, message_read


@pytest.fixture
def mode(monkeypatch):
    def set_(value):
        if value is None:
            monkeypatch.delenv(dm_access.SETTING, raising=False)
        else:
            monkeypatch.setenv(dm_access.SETTING, value)
    return set_


def test_default_is_full_and_an_unknown_word_is_the_strict_one(mode):
    mode(None)
    assert dm_access.mode() == "full"
    mode("everything")
    assert dm_access.mode() == "person"
    mode("  Metadata ")
    assert dm_access.mode() == "metadata"


def _msg(content="hello there", sender="bob"):
    return {"message_id": "m1", "sender_id": sender, "content": content, "created_at": "2026-01-01T00:00:00Z",
            "nonce": "n1", "msg_type": "chat"}


def _annotate(msgs, kind):
    message_read.annotate_messages(msgs, kind=kind, scope_id="dm_1", viewer_agent_id="me", private_key=None,
                                   pubkeys={}, verify=lambda m, k: None)


def test_full_opens_a_plaintext_dm_as_before(mode):
    mode("full")
    msgs = [_msg()]
    _annotate(msgs, "dm")
    assert msgs[0]["content"] == "hello there" and "withheld" not in msgs[0]


@pytest.mark.parametrize("word", ["metadata", "person"])
def test_stricter_modes_withhold_every_dm_text_but_keep_who_and_when(mode, word):
    mode(word)
    msgs = [_msg()]
    _annotate(msgs, "dm")
    m = msgs[0]
    assert m["content"] == "" and m["withheld"] == word and m["decrypted"] is False
    assert m["sender_id"] == "bob" and m["created_at"] and m["message_id"] == "m1"


def test_a_sealed_room_message_is_never_opened_in_a_stricter_mode_but_a_plain_room_message_is(mode, monkeypatch):
    mode("person")
    opened = []
    monkeypatch.setattr(crypto_box, "open_sealed", lambda *a, **k: opened.append(1) or "secret")
    sealed = _msg(content=json.dumps({"v": 2, "alg": "x", "ct": "AAAA"}))
    monkeypatch.setattr(crypto_box, "looks_like_any_envelope", lambda c: str(c).startswith('{"v"'))
    plain = _msg(content="public room talk")
    msgs = [sealed, plain]
    _annotate(msgs, "room")
    assert not opened, "no plaintext is made at all"
    assert msgs[0]["content"] == "" and msgs[0]["withheld"] == "person"
    assert msgs[1]["content"] == "public room talk"


def test_person_refuses_the_dm_tools_and_metadata_blanks_previews(mode):
    mode("person")
    out = dm_access.apply("get_dm_messages", {"success": True, "messages": [{"content": "x"}]})
    assert out["success"] is False and out["refused_by"] == "emporia-dm-access" and out["messages"] == []
    assert dm_access.apply("list_dm_threads", {"success": True, "threads": [{"last_content": "x"}]})["threads"] == []
    mode("metadata")
    out = dm_access.apply("list_dm_threads", {"success": True, "threads": [
        {"thread_id": "dm_1", "other_agent": "bob", "last_content": "secret", "last_message_at": "t"}]})
    t = out["threads"][0]
    assert t["last_content"] == "" and t["thread_id"] == "dm_1" and t["other_agent"] == "bob"
    out = dm_access.apply("get_dm_messages", {"success": True, "messages": [_msg()]})
    assert out["messages"][0]["content"] == ""


def test_inbox_events_of_dms_are_dropped_in_person_and_blanked_in_metadata(mode):
    inbox = {"success": True, "count": 3, "events": [
        {"inbox_id": 1, "event_type": "dm_received", "payload": {"type": "dm_received", "content": "secret",
                                                                  "sender_id": "bob", "encrypted": False}},
        {"inbox_id": 2, "event_type": "challenge", "payload": {"type": "challenge", "from": "bob"}},
        {"inbox_id": 3, "event_type": "room_message", "payload": {"type": "room_message", "content": "{ct}",
                                                                   "encrypted": True}}]}
    mode("full")
    assert dm_access.apply("get_inbox", inbox) is inbox
    mode("person")
    out = dm_access.apply("get_inbox", inbox)
    assert [e["inbox_id"] for e in out["events"]] == [2] and out["withheld_private_events"] == 2 and out["count"] == 1
    assert "secret" not in json.dumps(out)
    mode("metadata")
    out = dm_access.apply("get_inbox", inbox)
    assert len(out["events"]) == 3 and "secret" not in json.dumps(out)
    assert out["events"][0]["payload"]["sender_id"] == "bob"


def test_the_shape_of_every_answer_goes_through_it(mode):
    from emporia import mcp_surface
    mode("person")
    out = mcp_surface.shape("get_dm_messages", {"success": True, "messages": [_msg()]})
    assert out["success"] is False and "secret" not in json.dumps(out)


def test_sealed_posts_and_session_text_stay_sealed(mode, monkeypatch):
    from emporia import mcp_server as m
    mode("person")
    sealed = {"kind": "sealed", "text": "{ciphertext}", "author_id": "bob"}
    out = m._open_posts("me", [sealed, {"kind": "note", "text": "hi"}])
    assert out[0]["opened"] is False and out[0]["text"] == "" and out[0]["withheld"] == "person"
    assert out[1]["text"] == "hi"
    assert m._open_text("me", "sess_1", json.dumps({"v": 2}), ["chat"]) is None
