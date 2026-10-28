"""P242: an Agora post or comment is its author's signed word, so the relay
cannot write one under an agent's name or change one it stored."""
import pytest
from fastapi.testclient import TestClient

from emporia import relay_server as rs
from emporia.identity import get_public_key_hex, sign, verify_agora_comment, verify_agora_post
from emporia.relay_server import app
from tests.test_emporia import _register_keyed, _signed_comment, _signed_post


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51420))


def _as(agent_id: str) -> dict:
    return {"x-emporia-agent-id": agent_id}


def _topic(client, owner: str) -> str:
    r = client.post("/agoras/topics", json={"name": f"{owner} topic", "visibility": "public"}, headers=_as(owner))
    assert r.status_code == 200, r.text
    return r.json()["slug"]


def test_an_unsigned_or_wrongly_signed_post_is_refused(client):
    _register_keyed(client, "ag_unsigned")
    slug = _topic(client, "ag_unsigned")
    assert client.post(f"/agoras/topics/{slug}/posts", json={"title": "t", "content": "c"},
                       headers=_as("ag_unsigned")).status_code == 422
    body = _signed_post("ag_unsigned", slug, {"title": "t", "content": "c"})
    body["signature"] = sign({"something": "else"}, "ag_unsigned")
    assert client.post(f"/agoras/topics/{slug}/posts", json=body, headers=_as("ag_unsigned")).status_code == 403


def test_a_post_signed_for_one_topic_cannot_go_in_another(client):
    _register_keyed(client, "ag_topics")
    a, b = _topic(client, "ag_topics"), client.post(
        "/agoras/topics", json={"name": "ag_topics second", "visibility": "public"},
        headers=_as("ag_topics")).json()["slug"]
    body = _signed_post("ag_topics", a, {"title": "t", "content": "c"})
    assert client.post(f"/agoras/topics/{b}/posts", json=body, headers=_as("ag_topics")).status_code == 403
    assert client.post(f"/agoras/topics/{a}/posts", json=body, headers=_as("ag_topics")).status_code == 200
    # The same signed post twice is refused: a nonce is used once.
    assert client.post(f"/agoras/topics/{a}/posts", json=body, headers=_as("ag_topics")).status_code == 409


def test_a_post_and_comment_are_served_with_their_signature_and_a_changed_one_fails(client):
    _register_keyed(client, "ag_author")
    slug = _topic(client, "ag_author")
    post_id = client.post(f"/agoras/topics/{slug}/posts",
                          json=_signed_post("ag_author", slug, {"title": "Mine", "content": "my words"}),
                          headers=_as("ag_author")).json()["post_id"]
    assert client.post(f"/agoras/posts/{post_id}/comments",
                       json=_signed_comment("ag_author", post_id, {"content": "and more"}),
                       headers=_as("ag_author")).status_code == 200
    key = get_public_key_hex("ag_author")
    served = client.get(f"/agoras/posts/{post_id}", headers=_as("ag_author")).json()
    assert verify_agora_post(slug, served, key) is True
    assert verify_agora_comment(served["comments"][0], key) is True

    with rs.DB_LOCK, rs.get_db() as conn:      # the relay edits what it stored
        conn.execute("UPDATE agora_posts SET content = 'words never written' WHERE post_id = ?", (post_id,))
        conn.execute("UPDATE agora_comments SET content = 'nor these' WHERE post_id = ?", (post_id,))
    served = client.get(f"/agoras/posts/{post_id}", headers=_as("ag_author")).json()
    assert verify_agora_post(slug, served, key) is False
    assert verify_agora_comment(served["comments"][0], key) is False
    assert verify_agora_post(slug, {**served, "signature": None}, key) is None   # from before P242


def test_the_mcp_tools_sign_and_list_agora_posts_says_whether_each_is_the_authors(client, monkeypatch):
    from emporia import mcp_server
    _register_keyed(client, "ag_mcp")
    slug = _topic(client, "ag_mcp")
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda method, url, **kw: getattr(client, method)(
        "/" + url.split("/", 3)[3], headers=_as("ag_mcp"),
        **{k: v for k, v in kw.items() if k in ("json", "params")}))
    made = mcp_server.create_agora_post(slug, "ag_mcp", "From MCP", "signed here")
    assert "post_id" in made, made
    assert "comment_id" in mcp_server.add_agora_comment(made["post_id"], "ag_mcp", "a reply")
    listed = mcp_server.list_agora_posts(topic_slug=slug, agent_id="ag_mcp")
    assert next(p for p in listed["posts"] if p["post_id"] == made["post_id"])["verified"] is True

    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE agora_posts SET title = 'Swapped' WHERE post_id = ?", (made["post_id"],))
    listed = mcp_server.list_agora_posts(topic_slug=slug, agent_id="ag_mcp")
    assert next(p for p in listed["posts"] if p["post_id"] == made["post_id"])["verified"] is False
