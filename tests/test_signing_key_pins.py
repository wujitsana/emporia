"""P244: clients pin other agents' Ed25519 signing keys on first sight, so a
relay that serves a made-up key beside forged content gets `False` and a plain
`signing_key_changed`, not a pass."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from emporia import identity, mcp_server
from emporia import relay_server as rs
from emporia.agent_sdk import EmporiaAgent
from emporia.identity import get_public_key_hex
from emporia.relay_server import app
from tests.test_emporia import (
    _register_keyed,
    _send_dm_raw,
    _signed_room,
)
from tests.test_signed_agoras import _signed_post, _topic

KEY_A = "a" * 64
KEY_B = "b" * 64


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51440))


def _as(agent_id: str) -> dict:
    return {"x-emporia-agent-id": agent_id}


class _Served:
    def __init__(self, resp, key_for=None):
        self.status_code, self.text, self._resp, self._key_for = resp.status_code, resp.text, resp, key_for

    def json(self):
        body = self._resp.json()
        if self._key_for and isinstance(body, dict) and body.get("agent_id") in self._key_for:
            body = {**body, "public_key_hex": self._key_for[body["agent_id"]]}
        return body


@pytest.fixture
def relay(client, monkeypatch):
    """The MCP tools talking to the test relay, which can be made hostile: the
    keys it serves for some agents are replaced (`swap`)."""
    swap: dict[str, str] = {}

    def request(method, url, agent_id="", **kw):
        resp = getattr(client, method)("/" + url.split("/", 3)[3], headers=_as(agent_id or "pin_viewer"),
                                       **{k: v for k, v in kw.items() if k in ("json", "params")})
        return _Served(resp, swap)
    monkeypatch.setattr(mcp_server, "_bearer_request", request)
    return swap


# --- the pin itself --------------------------------------------------------

def test_first_sight_pins_and_a_swapped_key_gets_the_pinned_one_back():
    pid = "pin_unit@relay.example"
    assert identity.pin_peer_signing_key(pid, KEY_A, "sid1") == KEY_A
    assert identity.pin_peer_signing_key(pid, KEY_A, "sid1") == KEY_A
    assert identity.pin_peer_signing_key(pid, KEY_B, "sid1") == KEY_A         # pinned, not served
    assert identity.load_peer_signing_keys()[pid] == {"public_key_hex": KEY_A, "stable_id": "sid1"}
    # An empty or malformed served key falls back to the pin; with no pin it is "".
    assert identity.pin_peer_signing_key(pid, "", "") == KEY_A
    assert identity.pin_peer_signing_key(pid, "not-hex", "") == KEY_A
    assert identity.pin_peer_signing_key("pin_none@relay.example", "zz", "") == ""
    assert "pin_none@relay.example" not in identity.load_peer_signing_keys()


def test_a_changed_stable_id_counts_as_changed_and_a_missing_one_is_filled_in():
    changed: list[str] = []
    url = "https://relay.example"
    identity.pinned_signing_key("pin_sid", url, KEY_A, "", changed)
    identity.pinned_signing_key("pin_sid", url, KEY_A, "sidA", changed)      # learns the stable id
    assert changed == []
    identity.pinned_signing_key("pin_sid", url, KEY_A, "sidB", changed)      # same key, other identity
    assert changed == ["pin_sid"]


def test_trust_new_signing_key_is_the_human_chosen_repin():
    pid = "pin_repin@relay.example"
    identity.pin_peer_signing_key(pid, KEY_A)
    identity.pin_peer_signing_key(pid, KEY_B)
    assert identity.load_peer_signing_keys()[pid]["public_key_hex"] == KEY_A
    identity.trust_new_signing_key(pid, KEY_B)
    assert identity.pin_peer_signing_key(pid, KEY_B) == KEY_B
    with pytest.raises(ValueError):
        identity.trust_new_signing_key(pid, "short")


def test_namesakes_on_two_relays_do_not_collide():
    one = identity.signing_pin_id("pin_twin", "https://one.example")
    two = identity.signing_pin_id("pin_twin", "https://two.example")
    assert one == "pin_twin@one.example" and two == "pin_twin@two.example"
    assert identity.signing_pin_id("pin_twin@two.example", "https://one.example") == two   # an address keeps its host
    assert identity.signing_pin_id("pin_twin", "http://localhost:8088") == identity.signing_pin_id(
        "pin_twin", "http://127.0.0.1:8088")
    changed: list[str] = []
    assert identity.pinned_signing_key("pin_twin", "https://one.example", KEY_A, "", changed) == KEY_A
    assert identity.pinned_signing_key("pin_twin", "https://two.example", KEY_B, "", changed) == KEY_B
    assert changed == []


def test_a_corrupt_pin_file_is_set_aside_not_reset_silently(tmp_path):
    with patch.object(identity, "KEY_DIR", tmp_path):
        (tmp_path / "peer_signing_keys.json").write_text("{not json")
        assert identity.load_peer_signing_keys() == {}
        assert list(tmp_path.glob("peer_signing_keys.json.corrupt-*"))
        assert not (tmp_path / "peer_signing_keys.json").exists()


def test_a_pin_that_cannot_be_written_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with patch.object(identity, "KEY_DIR", blocker / "keys"):
        with pytest.raises(OSError):
            identity.pin_peer_signing_key("pin_unwritable@relay.example", KEY_A)


# --- through the tools -----------------------------------------------------

def test_a_forged_agora_post_under_a_swapped_key_fails_and_is_reported(client, relay):
    for a in ("pin_ag_victim", "pin_ag_evil", "pin_viewer"):
        _register_keyed(client, a)
    slug = _topic(client, "pin_ag_victim")
    for who, text in (("pin_ag_victim", "my words"), ("pin_ag_evil", "forged words")):
        assert client.post(f"/agoras/topics/{slug}/posts", json=_signed_post(who, slug, {"title": who, "content": text}),
                           headers=_as(who)).status_code == 200
    listed = mcp_server.list_agora_posts(topic_slug=slug, agent_id="pin_viewer")
    assert {p["author_id"]: p["verified"] for p in listed["posts"]} == {"pin_ag_victim": True, "pin_ag_evil": True}
    assert "signing_key_changed" not in listed

    # The relay now says the evil post is the victim's, and serves the evil key as the victim's.
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE agora_posts SET author_id = 'pin_ag_victim' WHERE title = 'pin_ag_evil'")
    relay["pin_ag_victim"] = get_public_key_hex("pin_ag_evil")
    listed = mcp_server.list_agora_posts(topic_slug=slug, agent_id="pin_viewer")
    forged = next(p for p in listed["posts"] if p["title"] == "pin_ag_evil")
    assert forged["verified"] is False
    assert listed["signing_key_changed"] == ["pin_ag_victim"]
    pin_id = identity.signing_pin_id("pin_ag_victim", mcp_server._base())
    assert identity.load_peer_signing_keys()[pin_id]["public_key_hex"] == get_public_key_hex("pin_ag_victim")

    # After a human re-pins, the new key is the one used.
    identity.trust_new_signing_key(pin_id, get_public_key_hex("pin_ag_evil"))
    listed = mcp_server.list_agora_posts(topic_slug=slug, agent_id="pin_viewer")
    assert next(p for p in listed["posts"] if p["title"] == "pin_ag_evil")["verified"] is True
    assert "signing_key_changed" not in listed


def test_check_room_reports_a_creator_key_the_relay_swapped(client, relay):
    for a in ("pin_rm_creator", "pin_rm_evil", "pin_viewer"):
        _register_keyed(client, a)
    ids = {}
    for who in ("pin_rm_creator", "pin_rm_evil"):
        r = client.post("/rooms", json=_signed_room(who, {"name": f"{who} room"}), headers=_as(who))
        assert r.status_code == 200, r.text
        ids[who] = r.json()["room_id"]
    honest = mcp_server.check_room(ids["pin_rm_creator"], agent_id="pin_viewer")
    assert honest["settings"] is True and "signing_key_changed" not in honest

    with rs.DB_LOCK, rs.get_db() as conn:     # the evil room is passed off as the creator's
        conn.execute("UPDATE rooms SET creator_id = 'pin_rm_creator' WHERE room_id = ?", (ids["pin_rm_evil"],))
    relay["pin_rm_creator"] = get_public_key_hex("pin_rm_evil")
    forged = mcp_server.check_room(ids["pin_rm_evil"], agent_id="pin_viewer")
    assert forged["settings"] is False
    assert forged["signing_key_changed"] == ["pin_rm_creator"]


def test_a_forged_dm_under_a_swapped_key_fails_and_is_reported(client, relay):
    for a in ("pin_dm_victim", "pin_dm_evil", "pin_viewer"):
        _register_keyed(client, a)
    threads = {}
    for peer in ("pin_dm_victim", "pin_dm_evil"):
        r = client.post("/dm/start", json={"to_agent": peer}, headers=_as("pin_viewer"))
        assert r.status_code == 200, r.text
        threads[peer] = r.json()["thread_id"]
        assert _send_dm_raw(client, threads[peer], peer, f"hello from {peer}").status_code == 200
    read = mcp_server.get_dm_messages(threads["pin_dm_victim"], agent_id="pin_viewer")
    assert [m["verified"] for m in read["messages"]] == [True] and "signing_key_changed" not in read

    with rs.DB_LOCK, rs.get_db() as conn:     # the evil message is relabelled as the victim's
        conn.execute("UPDATE dm_messages SET sender_id = 'pin_dm_victim' WHERE thread_id = ?",
                     (threads["pin_dm_evil"],))
    relay["pin_dm_victim"] = get_public_key_hex("pin_dm_evil")
    read = mcp_server.get_dm_messages(threads["pin_dm_evil"], agent_id="pin_viewer")
    assert [m["verified"] for m in read["messages"]] == [False]
    assert read["signing_key_changed"] == ["pin_dm_victim"]


def test_the_sdk_pins_too_and_flags_the_message_it_cannot_trust():
    served = {"public_key_hex": KEY_A, "stable_id": "sidX"}

    async def bearer(method, url, **kw):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: dict(served))
    agent = SimpleNamespace(relay_url="https://sdk.example", _bearer_request=bearer)

    async def run():
        changed: list[str] = []
        first = await EmporiaAgent._sender_public_key(agent, "pin_sdk_peer", changed)
        served["public_key_hex"] = KEY_B
        second = await EmporiaAgent._sender_public_key(agent, "pin_sdk_peer", changed)
        return first, second, changed
    first, second, changed = asyncio.run(run())
    assert (first, second, changed) == (KEY_A, KEY_A, ["pin_sdk_peer"])


def test_verify_session_checks_moves_against_the_pinned_key(client, relay):
    # verify_session fetches each participant's key through the same pinned lookup.
    _register_keyed(client, "pin_vs_player")
    _register_keyed(client, "pin_viewer")
    assert mcp_server._sender_public_key("pin_viewer", "pin_vs_player") == get_public_key_hex("pin_vs_player")
    relay["pin_vs_player"] = KEY_B
    changed: list[str] = []
    assert mcp_server._sender_public_key("pin_viewer", "pin_vs_player", changed) == get_public_key_hex("pin_vs_player")
    assert changed == ["pin_vs_player"]
    assert json.dumps(mcp_server._flag_changed_keys({}, changed)) == '{"signing_key_changed": ["pin_vs_player"]}'
