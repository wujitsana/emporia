"""P241: an open game in the lobby other relays read is its creator's signed
offer, checked by whoever reads it — not taken on the word of the relay serving it."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from emporia import wire
from emporia.identity import get_public_key_hex, sign, verify
from emporia.relay_server import app
from tests.test_emporia import _register, _register_keyed

@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51410))


def _as(agent_id: str) -> dict:
    return {"x-emporia-agent-id": agent_id}


def _session(client, creator: str, config: dict | None = None) -> str:
    r = client.post("/sessions", json={"module_type": "emporia:chess:v1", "config": config or {},
                                       "payment_rules": {"mode": "free"}}, headers=_as(creator))
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _offer_body(client, creator: str, session_id: str) -> dict:
    s = client.get(f"/sessions/{session_id}", headers=_as(creator)).json()
    signed = wire.signed_open_session_payload(
        wire.stable_id_for(get_public_key_hex(creator)), client.get("/health").json()["relay_url"],
        session_id, s["module_type"], s["config"], s["payment_rules"])
    return {"signature": sign(signed, creator)}


def _feed(client) -> dict:
    return client.get("/federation/v1/open-sessions").json()


def test_only_an_offered_game_is_in_the_lobby_and_its_offer_verifies(client):
    _register_keyed(client, "so_host")
    offered, quiet = _session(client, "so_host"), _session(client, "so_host")
    r = client.post(f"/sessions/{offered}/offer", json=_offer_body(client, "so_host", offered), headers=_as("so_host"))
    assert r.status_code == 200, r.text
    feed = _feed(client)
    ids = {x["session_id"] for x in feed["open_sessions"]}
    assert offered in ids and quiet not in ids
    item = next(x for x in feed["open_sessions"] if x["session_id"] == offered)
    assert wire.open_session_verdict(item, feed["origin_relay"], verify) == ""


def test_an_offer_must_be_the_creators_and_match_the_stored_terms(client):
    _register_keyed(client, "so_owner")
    _register_keyed(client, "so_other")
    sid = _session(client, "so_owner")
    assert client.post(f"/sessions/{sid}/offer", json=_offer_body(client, "so_owner", sid),
                       headers=_as("so_other")).status_code == 403
    assert client.post(f"/sessions/{sid}/offer", json={"signature": sign({"x": 1}, "so_owner")},
                       headers=_as("so_owner")).status_code == 403


def test_a_game_with_a_named_opponent_is_never_offered(client):
    _register_keyed(client, "so_named")
    _register(client, "so_friend")
    sid = _session(client, "so_named", {"opponent": "so_friend"})
    r = client.post(f"/sessions/{sid}/offer", json=_offer_body(client, "so_named", sid), headers=_as("so_named"))
    assert r.status_code == 400


@pytest.mark.parametrize("field,value", [
    ("payment_rules", {"mode": "x402", "stake_per_participant": "50"}),
    ("config", {"creator_plays": "black"}),
    ("module_type", "emporia:blotto:v1"),
    ("creator_stable_id", "00" * 32),
])
def test_a_relay_cannot_change_an_offered_games_terms(client, field, value):
    _register_keyed(client, f"so_t_{field}")
    sid = _session(client, f"so_t_{field}")
    client.post(f"/sessions/{sid}/offer", json=_offer_body(client, f"so_t_{field}", sid),
                headers=_as(f"so_t_{field}"))
    feed = _feed(client)
    honest = next(x for x in feed["open_sessions"] if x["session_id"] == sid)
    assert wire.open_session_verdict({**honest, field: value}, feed["origin_relay"], verify) != ""


def test_an_offer_names_the_relay_it_was_made_on(client):
    _register_keyed(client, "so_where")
    sid = _session(client, "so_where")
    client.post(f"/sessions/{sid}/offer", json=_offer_body(client, "so_where", sid), headers=_as("so_where"))
    item = next(x for x in _feed(client)["open_sessions"] if x["session_id"] == sid)
    assert wire.open_session_verdict(item, "https://another-relay.example", verify) != ""

def test_the_mcp_tool_offers_and_scan_relays_drops_a_forged_game(client, monkeypatch):
    from emporia import mcp_server
    _register_keyed(client, "so_mcp")
    monkeypatch.setattr(mcp_server, "RELAY_URL", "http://relay-under-test")
    monkeypatch.setattr(mcp_server, "_linked_relays", lambda: [])
    monkeypatch.setattr(mcp_server, "_bearer_request", lambda method, url, **kw: getattr(client, method)(
        "/" + url.split("/", 3)[3], headers=_as("so_mcp"),
        **{k: v for k, v in kw.items() if k in ("json", "params")}))

    def fake_get(url, timeout=10, **kw):
        return client.get(url[len("http://relay-under-test"):])

    with patch("httpx.get", fake_get):
        made = mcp_server.create_session("emporia:chess:v1", agent_id="so_mcp")
        assert made["success"] and made["offered"], made
        honest = mcp_server.scan_relays(kind="game")
    assert made["session_id"] in {x["id"] for x in honest["results"]}

    # A relay that alters an offer and signs the altered feed with its own key.
    from emporia.routes import federation

    original = federation._lobby_items

    def tampered():
        return [{**x, "payment_rules": {"mode": "x402", "stake_per_participant": "99"}}
                if x["session_id"] == made["session_id"] else x for x in original()]

    with patch.object(federation, "_lobby_items", tampered), patch("httpx.get", fake_get):
        forged = mcp_server.scan_relays(kind="game")
    assert made["session_id"] not in {x["id"] for x in forged["results"]}
    assert forged["refused_unsigned_or_forged"]["http://relay-under-test"] >= 1
