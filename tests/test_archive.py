"""P194: an agent keeps its own copy of everything it is part of on a relay."""
import json

import pytest
from fastapi.testclient import TestClient

from emporia import wire
from emporia.identity import sign
from emporia.relay_server import app
from tests.test_emporia import _register_keyed


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51300))


def _h(a):
    return {"x-emporia-agent-id": a}


def test_archive_keeps_dms_rooms_posts_listings_and_sessions_and_only_fetches_whats_new(client, monkeypatch,
                                                                                         tmp_path):
    from emporia import identity, mcp_server
    for a in ("ar_me", "ar_friend"):
        _register_keyed(client, a)
    thread = client.post("/dm/start", json={"to_agent": "ar_friend", "encrypted": False},
                         headers=_h("ar_me")).json()["thread_id"]
    nonce = wire.new_message_nonce()
    client.post(f"/dm/{thread}/send", headers=_h("ar_friend"), json={
        "content": "hi", "msg_type": "chat", "nonce": nonce,
        "signature": sign(wire.signed_dm_thread_payload(thread, "hi", "chat", nonce), "ar_friend")})
    room = client.post("/rooms", json={"name": "Archive room"}, headers=_h("ar_me")).json()["room_id"]
    n2 = wire.new_message_nonce()
    client.post(f"/rooms/{room}/message", headers=_h("ar_me"), json={
        "content": "room line", "msg_type": "chat", "nonce": n2,
        "signature": sign(wire.signed_room_message_payload(room, "room line", "chat", n2), "ar_me")})
    slug = client.post("/agoras/topics", json={"name": "Archive topic"}, headers=_h("ar_me")).json()["slug"]
    client.post(f"/agoras/topics/{slug}/posts", json={"title": "t", "content": "mine"}, headers=_h("ar_me"))
    client.post("/listings", json={"title": "Archive listing", "listing_type": "service"}, headers=_h("ar_me"))
    client.post("/sessions", json={"module_type": "emporia:chess:v1", "creator_agent_id": "ar_me", "config": {},
                                   "payment_rules": {"mode": "free"}}, headers=_h("ar_me"))

    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "_bearer_request",
                        lambda method, url, **kw: getattr(client, method)(
                            "/" + url.split("/", 3)[3], headers=_h("ar_me"),
                            **{k: v for k, v in kw.items() if k in ("json", "params")}))
    first = mcp_server.archive_now(agent_id="ar_me")
    assert first["success"], first
    saved = first["saved"]
    assert saved["dm_messages"] == 1 and saved["room_messages"] == 1 and saved["posts"] == 1
    assert saved["listings"] >= 1 and saved["sessions"] >= 1
    root = tmp_path / "archive"
    dm = next(root.glob(f"*/dms/{thread}.jsonl"))
    assert json.loads(dm.read_text().splitlines()[0])["content"] == "hi"
    assert oct(dm.stat().st_mode)[-3:] == "600"
    again = mcp_server.archive_now(agent_id="ar_me")
    assert again["saved"].get("dm_messages", 0) == 0 and again["saved"].get("room_messages", 0) == 0


def test_an_agent_leaves_a_relay_only_once_nothing_is_open_there(client):
    """P195."""
    _register_keyed(client, "lv_agent")
    sess = client.post("/sessions", json={"module_type": "emporia:chess:v1", "creator_agent_id": "lv_agent",
                                          "config": {}, "payment_rules": {"mode": "free"}},
                       headers=_h("lv_agent")).json()["session_id"]
    client.post("/listings", json={"title": "Leaving", "listing_type": "service"}, headers=_h("lv_agent"))
    r = client.post("/agents/lv_agent/leave", headers=_h("lv_agent"))
    assert r.status_code == 409 and sess in r.json()["detail"]["open_sessions"]
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE sessions SET status = 'abandoned' WHERE session_id = ?", (sess,))
    r = client.post("/agents/lv_agent/leave", headers=_h("lv_agent"))
    assert r.status_code == 200 and r.json()["listings_removed"] == 1
    assert client.post("/listings", json={"title": "x", "listing_type": "service"},
                       headers=_h("lv_agent")).status_code in (401, 403)
    assert client.get("/agents/lv_agent/card").status_code == 404


def test_deleting_an_agent_moves_its_profile_to_a_private_trash(monkeypatch, tmp_path, capsys):
    """P195: and only a person, who types its name, does it."""
    from emporia import cli
    profile = tmp_path / "profiles" / "goner"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("mcp_servers:\n  emporia:\n    env:\n      EMPORIA_AGENT_ID: goner\n")
    monkeypatch.setattr(cli, "profiles_root", lambda: tmp_path / "profiles")
    monkeypatch.setattr(cli, "_agent_profiles", lambda root: [(profile, {"env": {"EMPORIA_AGENT_ID": "goner"}})])
    monkeypatch.setattr(cli, "_mcp_env", lambda p: {"EMPORIA_RELAY_URL": "http://relay.test"})
    monkeypatch.setattr(cli, "_wallet_address", lambda *a: "")
    left = []
    monkeypatch.setattr(cli, "_as_agent", lambda prof, code, **kw: __import__("subprocess").CompletedProcess(
        [], 0, stdout=left.append(code) or '{"success": true}', stderr=""))
    monkeypatch.setattr(cli, "_persons_terminal", lambda: True)
    monkeypatch.setattr(cli, "_TTY", True)
    monkeypatch.setattr("builtins.input", lambda *_: "nope")
    assert cli.main(["agent", "delete", "goner"]) == 1 and profile.exists()
    monkeypatch.setattr("builtins.input", lambda *_: "goner")
    assert cli.main(["agent", "delete", "goner"]) == 0
    assert not profile.exists() and left and "leave_relay_as('http://relay.test')" in left[0]
    trashed = next((tmp_path / "profiles" / ".deleted").iterdir())
    assert trashed.name.startswith("goner-") and oct(trashed.stat().st_mode)[-3:] == "700"
    monkeypatch.setattr(cli, "_persons_terminal", lambda: False)
    assert cli.main(["agent", "delete", "goner"]) == 1


def test_the_agent_reads_its_own_archive_knows_its_settings_and_asks_for_cross_relay_dms(client, monkeypatch,
                                                                                         tmp_path):
    """P195: what an agent can do itself, and what it asks its operator for."""
    from emporia import identity, mcp_server
    _register_keyed(client, "al_me")
    _register_keyed(client, "al_you")
    thread = client.post("/dm/start", json={"to_agent": "al_you", "encrypted": False},
                         headers=_h("al_me")).json()["thread_id"]
    nonce = wire.new_message_nonce()
    client.post(f"/dm/{thread}/send", headers=_h("al_you"), json={
        "content": "keep this", "msg_type": "chat", "nonce": nonce,
        "signature": sign(wire.signed_dm_thread_payload(thread, "keep this", "chat", nonce), "al_you")})
    keys = tmp_path / "keys"
    keys.mkdir()
    monkeypatch.setattr(identity, "KEY_DIR", keys)
    monkeypatch.setattr(mcp_server, "_bearer_request",
                        lambda method, url, **kw: getattr(client, method)(
                            "/" + url.split("/", 3)[3], headers=_h("al_me"),
                            **{k: v for k, v in kw.items() if k in ("json", "params")}))
    assert mcp_server.archive_now(agent_id="al_me")["success"]
    got = mcp_server.read_archive("dm", thread, agent_id="al_me")
    assert got["success"] and [m["content"] for m in got["messages"]] == ["keep this"]
    assert not mcp_server.read_archive("dm", "../../etc", agent_id="al_me")["success"]

    settings = mcp_server.my_settings(agent_id="al_me")
    assert settings["visit_policy"] == "ask" and settings["takes_dms_from_other_relays"] is False
    assert "emporia visits al_me" in settings["how_to_change"]["where it may go"]

    monkeypatch.setattr(mcp_server, "_ask_operator", lambda message, title="": False)
    assert not mcp_server.set_cross_relay_dms(True, agent_id="al_me")["success"]
    monkeypatch.setattr(mcp_server, "_ask_operator", lambda message, title="": True)
    assert mcp_server.set_cross_relay_dms(True, agent_id="al_me")["success"]
    assert mcp_server.my_settings(agent_id="al_me")["takes_dms_from_other_relays"] is True
    monkeypatch.setattr(mcp_server, "_ask_operator", lambda message, title="": False)
    assert mcp_server.set_cross_relay_dms(False, agent_id="al_me")["success"]   # off needs no yes
