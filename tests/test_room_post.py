

def test_send_room_message_other_takes_a_label_and_says_what_to_pass(monkeypatch):
    from emporia import mcp_server as m
    monkeypatch.setattr(m, "AGENT_ID", "someone")
    no = m.send_room_message("room_x", "a digest of the week", kind="other")
    assert no["success"] is False and 'label="digest"' in no["error"] and "kind=\"note\"" in no["error"]
    stray = m.send_room_message("room_x", "hi", kind="note", label="digest")
    assert stray["success"] is False and "only for kind" in stray["error"]
    seen = {}

    def fake_prepare(kind, content, tags, reply_to, quote_of, label="", *, sealed=False, root=None):
        seen["label"] = label
        raise ValueError("stop here")
    from emporia import room_post
    monkeypatch.setattr(room_post, "prepare", fake_prepare)

    class R:
        status_code = 200
        text = ""
        content = b"{}"

        def json(self):
            return {"room_id": "room_x", "encrypted": False, "creator_id": "c"}
    monkeypatch.setattr(m, "_bearer_request", lambda *a, **k: R())
    monkeypatch.setattr(m, "_sender_public_key", lambda *a, **k: "00" * 32)
    from emporia import identity
    monkeypatch.setattr(identity, "verified_room_encryption", lambda *a, **k: False)
    m.send_room_message("room_x", "a digest", kind="other", label="digest")
    assert seen["label"] == "digest"
