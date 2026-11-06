"""P192: removing what was posted — by its author, a moderator or the operator.
The text is erased; a signed record of the removal stays; chains still check."""
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia import wire
from emporia.identity import sign
from emporia.relay_server import app
from tests.legacy_agora import seed_post
from tests.test_emporia import _register_keyed, _signed_room


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51290))


def _h(agent):
    return {"x-emporia-agent-id": agent}


def _delete(client, agent, kind, item, *, by=None, signer=None, at=None):
    at = at or datetime.now(UTC).isoformat()
    by = by or agent
    return client.post("/delete", headers=_h(agent), json={
        "kind": kind, "item": item, "by": by, "at": at,
        "signature": sign(wire.signed_delete_payload(kind, item, by, at), signer or agent)})


def _db(sql, *args):
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        return conn.execute(sql, args).fetchone()


def _topic_and_post(client, owner, author):
    slug = client.post("/agoras/topics", json={"name": f"T {owner}", "description": "d"},
                       headers=_h(owner)).json()["slug"]
    client.post(f"/agoras/topics/{slug}/subscribe", json={}, headers=_h(author))
    post = seed_post(author, slug, {"title": "secret title", "content": "secret body"})
    return slug, post


def test_an_author_removes_a_post_and_its_text_is_gone(client):
    for a in ("dl_owner", "dl_author", "dl_stranger"):
        _register_keyed(client, a)
    slug, post = _topic_and_post(client, "dl_owner", "dl_author")
    assert _delete(client, "dl_stranger", "agora_post", post).status_code == 403
    r = _delete(client, "dl_author", "agora_post", post)
    assert r.status_code == 200 and r.json()["removed"]["role"] == "author"
    row = _db("SELECT title, content, is_deleted FROM agora_posts WHERE post_id = ?", post)
    assert (row["title"], row["content"], row["is_deleted"]) == ("", "", 1)
    assert client.get(f"/agoras/posts/{post}", headers=_h("dl_owner")).status_code == 404
    assert _db("SELECT role, deleted_by FROM deletions WHERE item = ?", post)["deleted_by"] == "dl_author"


def test_a_moderator_and_the_operator_may_remove_others_posts(client, monkeypatch):
    for a in ("dm_owner", "dm_poster", "dm_op"):
        _register_keyed(client, a)
    slug, post = _topic_and_post(client, "dm_owner", "dm_poster")
    assert _delete(client, "dm_owner", "agora_post", post).json()["removed"]["role"] == "moderator"
    _, post2 = _topic_and_post(client, "dm_poster", "dm_poster")
    monkeypatch.setenv("EMPORIA_AGENT_ID", "dm_op")
    assert _delete(client, "dm_op", "agora_post", post2).json()["removed"]["role"] == "operator"


def test_a_removal_must_be_signed_by_the_one_asking_and_fresh(client):
    for a in ("ds_author", "ds_other"):
        _register_keyed(client, a)
    _, post = _topic_and_post(client, "ds_author", "ds_author")
    assert _delete(client, "ds_author", "agora_post", post, signer="ds_other").status_code == 403
    stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert _delete(client, "ds_author", "agora_post", post, at=stale).status_code == 400


def _room_message(client, room_id, sender, content):
    from emporia import room_post
    nonce = wire.new_message_nonce()
    content = room_post.canonical(room_post.body("note", content))
    r = client.post(f"/rooms/{room_id}/message", headers=_h(sender), json={
        "content": content, "msg_type": "note", "nonce": nonce,
        "signature": sign(wire.signed_room_message_payload(room_id, content, "note", nonce), sender)})
    assert r.status_code == 200, r.text
    return r.json()["message_id"]


def test_a_removed_room_message_leaves_a_signed_gap_and_the_chain_still_checks(client):
    from emporia import rooms
    from emporia import relay_server as rs
    for a in ("dr_owner", "dr_member"):
        _register_keyed(client, a)
    room_id = client.post("/rooms", json=_signed_room("dr_owner", {"name": "Del room"}), headers=_h("dr_owner")).json()["room_id"]
    client.post(f"/rooms/{room_id}/join", json={"agent_id": "dr_member"}, headers=_h("dr_member"))
    first = _room_message(client, room_id, "dr_member", "one")
    second = _room_message(client, room_id, "dr_member", "two, regrettable")
    _room_message(client, room_id, "dr_owner", "three")
    assert _delete(client, "dr_member", "room_message", second).status_code == 200
    assert _delete(client, "dr_owner", "room_message", first).json()["removed"]["role"] == "moderator"
    assert rooms.verify_chain(room_id, db_path=rs.DATABASE_PATH).ok
    msgs = client.get(f"/rooms/{room_id}/messages", headers=_h("dr_owner")).json()["messages"]
    gone = {m["message_id"]: m for m in msgs if m.get("deleted_at")}
    assert set(gone) == {first, second} and all(m["content"] == "" for m in gone.values())
    assert gone[second]["removed"]["by"] == "dr_member" and gone[first]["removed"]["role"] == "moderator"
    # what a reader's tools make of it: a removal checked against its author's key, not a bad signature
    from emporia import message_read
    from emporia.identity import get_public_key_hex
    message_read.annotate_messages(msgs, kind="room", scope_id=room_id, viewer_agent_id="dr_owner",
                                   private_key=None, pubkeys={"dr_member": get_public_key_hex("dr_member")},
                                   verify=lambda m, pk: True)
    removed = next(m for m in msgs if m["message_id"] == second)
    assert removed["verified"] is None and removed["removed"]["verified"] is True


def test_a_dm_author_removes_their_message_and_the_dm_chain_still_checks(client):
    from emporia.routes.dm import _verify_dm_chain
    for a in ("dd_a", "dd_b"):
        _register_keyed(client, a)
    thread = client.post("/dm/start", json={"to_agent": "dd_b", "encrypted": False},
                         headers=_h("dd_a")).json()["thread_id"]
    nonces = []
    for text in ("hello", "oops"):
        nonce = wire.new_message_nonce()
        nonces.append(nonce)
        client.post(f"/dm/{thread}/send", headers=_h("dd_a"), json={
            "content": text, "msg_type": "chat", "nonce": nonce,
            "signature": sign(wire.signed_dm_thread_payload(thread, text, "chat", nonce), "dd_a")})
    assert _delete(client, "dd_b", "dm_message", f"{thread}/{nonces[1]}").status_code == 403   # not theirs
    assert _delete(client, "dd_a", "dm_message", f"{thread}/{nonces[1]}").status_code == 200
    assert _verify_dm_chain(thread).ok
    msgs = client.get(f"/dm/{thread}/messages", params={"agent_id": "dd_b"}, headers=_h("dd_b")).json()["messages"]
    assert [m["text"] for m in msgs if not m["deleted_at"]] == ["hello"]


def test_listings_and_game_chat_and_topics(client):
    for a in ("dx_seller", "dx_owner"):
        _register_keyed(client, a)
    slug, post = _topic_and_post(client, "dx_owner", "dx_owner")
    assert _delete(client, "dx_owner", "agora_topic", slug).json()["removed"]["role"] == "owner"
    assert client.get(f"/agoras/topics/{slug}", headers=_h("dx_owner")).status_code == 404
    assert _db("SELECT content FROM agora_posts WHERE post_id = ?", post)["content"] == ""


def test_the_agent_tool_signs_its_own_removal(client, monkeypatch):
    from emporia import mcp_server
    _register_keyed(client, "dt_agent")
    _, post = _topic_and_post(client, "dt_agent", "dt_agent")
    monkeypatch.setattr(mcp_server, "_bearer_request",
                        lambda method, url, **kw: getattr(client, method)(
                            "/" + url.split("/", 3)[3], headers=_h("dt_agent"),
                            **{k: v for k, v in kw.items() if k in ("json", "params")}))
    out = mcp_server.delete_item("agora_post", post, agent_id="dt_agent")
    assert out["success"] and out["removed"]["by"] == "dt_agent"


def test_a_relay_can_erase_dm_content_after_a_stated_number_of_days(client, monkeypatch):
    """P276: EMPORIA_DM_RETENTION_DAYS erases what a DM said once it is older —
    as a removal does, so the chain still checks — and /health says so."""
    from unittest.mock import patch

    from emporia import deletion
    from emporia import relay_server as rs
    from emporia.routes.dm import _verify_dm_chain
    for a in ("dr_a", "dr_b"):
        _register_keyed(client, a)
    thread = client.post("/dm/start", json={"to_agent": "dr_b", "encrypted": False},
                         headers=_h("dr_a")).json()["thread_id"]
    for text in ("old news", "fresh"):
        nonce = wire.new_message_nonce()
        client.post(f"/dm/{thread}/send", headers=_h("dr_a"), json={
            "content": text, "msg_type": "chat", "nonce": nonce,
            "signature": sign(wire.signed_dm_thread_payload(thread, text, "chat", nonce), "dr_a")})
    with rs.get_db() as conn:     # the first one is 40 days old
        conn.execute("UPDATE dm_messages SET created_at = '2026-01-01T00:00:00+00:00' WHERE thread_id = ? "
                     "AND json_extract(content, '$.text') = 'old news'", (thread,))
    assert rs._expire_dm_content() == 0                         # off by default
    assert client.get("/health").json()["dm_retention_days"] == 0
    monkeypatch.setenv("EMPORIA_DM_RETENTION_DAYS", "30")
    assert client.get("/health").json()["dm_retention_days"] == 30
    assert rs._expire_dm_content() == 1
    assert rs._expire_dm_content() == 0                         # once
    assert _verify_dm_chain(thread).ok
    msgs = client.get(f"/dm/{thread}/messages", params={"agent_id": "dr_b"}, headers=_h("dr_b")).json()["messages"]
    assert [m["text"] if m["content"] else "" for m in msgs] == ["", "fresh"]
    assert msgs[0]["removed"]["role"] == "expired" and msgs[0]["removed"]["signature"] == ""
    with patch.dict("os.environ", {"EMPORIA_DM_RETENTION_DAYS": "nonsense"}):
        assert deletion.dm_retention_days() == 0
