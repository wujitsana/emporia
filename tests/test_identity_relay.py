"""The relay side of identity v2: look-alike names, display names, token revocation, the WebSocket token,
declared-model commitments and reveals, results by model."""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from emporia import identity, model_commit, wire
from emporia import relay_server as rs
from emporia.relay_server import app
from tests.ids import stable_of
from tests.test_emporia import _register, _register_keyed, _test_pubkey, bearer_for, prod_client  # noqa: F401

FOOLS_MATE = [("w", "f2f3"), ("b", "e7e5"), ("w", "g2g4"), ("b", "d8h4")]


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51217))


def _hdr(agent):
    return {"x-emporia-agent-id": agent}


# ── names ────────────────────────────────────────────────────────────────────

def test_a_lookalike_of_an_existing_or_reserved_handle_is_refused(client):
    name = f"alice{uuid.uuid4().hex[:4]}"
    assert client.post("/agents/register", json={"agent_id": name, "public_key_hex": _test_pubkey(name)}).status_code == 200
    twin = name.replace("l", "1", 1)                                  # a1ice...
    r = client.post("/agents/register", json={"agent_id": twin, "public_key_hex": _test_pubkey(twin)})
    assert r.status_code == 400 and "too close" in r.text
    r = client.post("/agents/register", json={"agent_id": "adm1n", "public_key_hex": _test_pubkey("adm1n")})
    assert r.status_code == 400 and "too close" in r.text
    r = client.post("/agents/register", json={"agent_id": "hermes", "public_key_hex": _test_pubkey("hermes")})
    assert r.status_code == 400 and "reserved" in r.text
    other = f"carol{uuid.uuid4().hex[:4]}"
    assert client.post("/agents/register", json={"agent_id": other, "public_key_hex": _test_pubkey(other)}).status_code == 200


def test_a_rename_to_a_lookalike_is_refused(client, prod_client):  # noqa: F811
    _register_keyed(prod_client, "rn_owner_x")
    _register_keyed(prod_client, "rn_victim")
    r = prod_client.post("/agents/rn_owner_x/rename", json={"new_agent_id": "rn_vict1m"},
                         headers=bearer_for(prod_client, "rn_owner_x"))
    assert r.status_code in (400, 403, 409) and "too close" in r.text


def test_display_names_are_validated(client):
    for bad in ("x" * 65, "a‮b", "two\nlines", "visit https://evil.example"):
        name = f"dn_{uuid.uuid4().hex[:8]}"
        r = client.post("/agents/register", json={"agent_id": name, "public_key_hex": _test_pubkey(name),
                                                  "display_name": bad})
        assert r.status_code == 400, bad
    name = f"dn_{uuid.uuid4().hex[:8]}"
    r = client.post("/agents/register", json={"agent_id": name, "public_key_hex": _test_pubkey(name),
                                              "display_name": "Plain Name"})
    assert r.status_code == 200 and r.json()["display_name"] == "Plain Name"


def test_a_view_carries_name_at_host(client):
    _register(client, "addr_viewer")
    agents = client.get("/agents", headers=_hdr("addr_viewer")).json()["agents"]
    assert all("@" in a["address"] for a in agents)


# ── tokens ───────────────────────────────────────────────────────────────────

def test_sign_out_revokes_the_token_and_revoke_all_every_one(prod_client):  # noqa: F811
    a = "tok_agent"
    _register_keyed(prod_client, a)
    h1, h2, h3 = (bearer_for(prod_client, a) for _ in range(3))
    assert prod_client.get(f"/agents/{a}", headers=h1).status_code == 200
    assert prod_client.post(f"/agents/{a}/sign-out", headers=h1).status_code == 200
    assert prod_client.get(f"/agents/{a}", headers=h1).status_code == 401, "the signed-out token is dead"
    assert prod_client.get(f"/agents/{a}", headers=h2).status_code == 200, "another token still works"
    r = prod_client.post(f"/agents/{a}/revoke-tokens", headers=h2)
    assert r.status_code == 200 and r.json()["valid_after"]
    assert prod_client.get(f"/agents/{a}", headers=h2).status_code == 401
    assert prod_client.get(f"/agents/{a}", headers=h3).status_code == 401
    fresh = bearer_for(prod_client, a)                       # signing in again works
    assert prod_client.get(f"/agents/{a}", headers=fresh).status_code == 200


def test_no_one_signs_out_or_revokes_another(prod_client):  # noqa: F811
    _register_keyed(prod_client, "tok_one")
    _register_keyed(prod_client, "tok_two")
    h = bearer_for(prod_client, "tok_two")
    assert prod_client.post("/agents/tok_one/sign-out", headers=h).status_code == 403
    assert prod_client.post("/agents/tok_one/revoke-tokens", headers=h).status_code == 403


def test_a_websocket_takes_its_token_in_the_subprotocol_and_never_in_the_url(prod_client):  # noqa: F811
    a = "ws_agent"
    _register_keyed(prod_client, a)
    token = bearer_for(prod_client, a)["Authorization"].split(" ", 1)[1]
    with prod_client.websocket_connect(f"/ws/agent/{a}", subprotocols=["emporia.v1", f"emporia.token.{token}"]) as ws:
        assert ws.accepted_subprotocol == "emporia.v1", "the token subprotocol is never echoed"
    with pytest.raises(WebSocketDisconnect):
        with prod_client.websocket_connect(f"/ws/agent/{a}?token={token}"):
            pass


# ── declared models ──────────────────────────────────────────────────────────

def _move(client, session_id, agent, uci, *, model=None, salt=None, tamper=False):
    step = client.get(f"/sessions/{session_id}", headers=_hdr(agent)).json()["step_number"]
    commit = model_commit.commit(model, salt) if model else ""
    payload = wire.signed_action_payload(stable_of(agent), "move", {"uci": uci}, "Playing the next move in the line",
                                         session_id, step, model_commit=commit)
    body = wire.action_envelope(agent, "move", {"uci": uci}, "Playing the next move in the line",
                                identity.sign(payload, agent), model_commit=commit)
    if tamper:
        body["model_commit"] = model_commit.commit("someone-else", "salt-salt-salt")
    return client.post(f"/sessions/{session_id}/action", json=body)


def _reveal(client, session_id, agent, reveals):
    at = datetime.now(UTC).isoformat()
    sig = identity.sign(wire.signed_model_reveal_payload(session_id, stable_of(agent), reveals, at), agent)
    return client.post(f"/sessions/{session_id}/model-reveal", headers=_hdr(agent),
                       json={"reveals": reveals, "at": at, "signature": sig})


def _chess_game(client):
    white, black = f"mw_{uuid.uuid4().hex[:8]}", f"mb_{uuid.uuid4().hex[:8]}"
    _register_keyed(client, white)
    _register_keyed(client, black)
    sid = client.post("/sessions", json={"module_type": "emporia:chess:v1"}, headers=_hdr(white)).json()["session_id"]
    assert client.post(f"/sessions/{sid}/join", json={"agent_id": black}, headers=_hdr(black)).status_code == 200
    return sid, {"w": white, "b": black}


def test_a_commitment_is_checked_at_the_end_and_a_bad_reveal_is_shown_never_scored(client):
    sid, p = _chess_game(client)
    salts = {"w": ["w-salt-0000", "w-salt-0002"], "b": ["b-salt-0001", "b-salt-0003"]}
    plan = [("w", "f2f3", 0), ("b", "e7e5", 0), ("w", "g2g4", 1), ("b", "d8h4", 1)]
    for side, uci, k in plan:
        r = _move(client, sid, p[side], uci, model="nous/hermes-4" if side == "w" else "deepseek/v4",
                  salt=salts[side][k])
        assert r.status_code == 200, r.text
    models = client.get(f"/sessions/{sid}/models", headers=_hdr(p["w"])).json()
    assert models["ended"] and models["declared"]
    assert models["players"][p["w"]] == {**models["players"][p["w"]], "status": "missing", "model": None}
    # a reveal before the end is refused; here the game is over (checkmate). White reveals right, black wrong
    good = [{"step": 0, "model": "nous/hermes-4", "salt": salts["w"][0]},
            {"step": 2, "model": "nous/hermes-4", "salt": salts["w"][1]}]
    r = _reveal(client, sid, p["w"], good)
    assert r.status_code == 200 and r.json()["status"] == "revealed" and r.json()["model"] == "nous/hermes-4"
    bad = [{"step": 1, "model": "gpt-lie", "salt": salts["b"][0]}, {"step": 3, "model": "deepseek/v4", "salt": salts["b"][1]}]
    r = _reveal(client, sid, p["b"], bad)
    assert r.json()["status"] == "mismatch" and r.json()["model"] is None
    shown = client.get(f"/sessions/{sid}/models", headers=_hdr(p["w"])).json()["players"]
    assert shown[p["w"]]["status"] == "revealed" and shown[p["w"]]["model"] == "nous/hermes-4"
    assert shown[p["b"]]["status"] == "mismatch"
    # the outcome is the board's, whatever was revealed
    with rs.DB_LOCK, rs.get_db() as conn:
        assert conn.execute("SELECT winner_id FROM settlements WHERE session_id = ?", (sid,)).fetchone()["winner_id"] == p["b"]
    # a stranger cannot reveal, and a reveal needs the player's own signature
    _register_keyed(client, "mstranger")
    assert _reveal(client, sid, "mstranger", good).status_code == 403
    at = datetime.now(UTC).isoformat()
    forged = identity.sign(wire.signed_model_reveal_payload(sid, stable_of(p["w"]), good, at), p["b"])
    r = client.post(f"/sessions/{sid}/model-reveal", headers=_hdr(p["w"]), json={"reveals": good, "at": at, "signature": forged})
    assert r.status_code == 403


def test_a_game_that_is_not_over_takes_no_reveal_and_shows_no_model(client):
    sid, p = _chess_game(client)
    assert _move(client, sid, p["w"], "e2e4", model="nous/hermes-4", salt="w-salt-0000").status_code == 200
    r = _reveal(client, sid, p["w"], [{"step": 0, "model": "nous/hermes-4", "salt": "w-salt-0000"}])
    assert r.status_code == 409
    shown = client.get(f"/sessions/{sid}/models", headers=_hdr(p["w"])).json()
    assert not shown["ended"] and shown["players"][p["w"]]["status"] == "committed" and shown["players"][p["w"]]["model"] is None
    assert shown["players"][p["b"]]["status"] == "none"


def test_a_bad_commit_is_refused_and_a_move_without_one_is_fine(client):
    sid, p = _chess_game(client)
    step = client.get(f"/sessions/{sid}", headers=_hdr(p["w"])).json()["step_number"]
    payload = wire.signed_action_payload(stable_of(p["w"]), "move", {"uci": "e2e4"}, "Opening with the king pawn",
                                         sid, step, model_commit="nothex")
    body = wire.action_envelope(p["w"], "move", {"uci": "e2e4"}, "Opening with the king pawn",
                                identity.sign(payload, p["w"]), model_commit="nothex")
    assert client.post(f"/sessions/{sid}/action", json=body).status_code == 400
    assert _move(client, sid, p["w"], "e2e4").status_code == 200                 # declared nothing
    assert _move(client, sid, p["b"], "e7e5", model="m", salt="salt-salt-s", tamper=True).status_code == 403


def test_results_by_model_count_wins_losses_and_draws_per_declared_model():
    from emporia.routes.models import results_by_model
    games = [{"session_id": "s1", "participants": ["a", "b"], "winner": "a"},
             {"session_id": "s2", "participants": ["a", "b"], "winner": "b"},
             {"session_id": "s3", "participants": ["a", "b"], "winner": None, "tied": True},
             {"session_id": "s4", "participants": ["a", "b"], "abandoned_by": "b"}]
    model_of = {("s1", "a"): "m1", ("s1", "b"): "m2", ("s2", "a"): "m1", ("s2", "b"): "m2",
                ("s3", "a"): "m1", ("s4", "a"): "m1", ("s4", "b"): "m2"}
    out = {r["model"]: r for r in results_by_model(games, model_of)}
    assert (out["m1"]["games"], out["m1"]["wins"], out["m1"]["losses"], out["m1"]["draws"]) == (3, 1, 1, 1)
    assert (out["m2"]["games"], out["m2"]["wins"], out["m2"]["losses"], out["m2"]["draws"]) == (3, 1, 2, 0)


def test_the_stats_route_serves_models_and_clients_without_names(client):
    body = {"agent_id": f"cl_{uuid.uuid4().hex[:6]}", "public_key_hex": None, "client_name": "sdk", "client_version": "1"}
    body["public_key_hex"] = _test_pubkey(body["agent_id"])
    assert client.post("/agents/register", json=body).status_code == 200
    d = client.get("/stats/models").json()
    assert d["declared"] is True and d["clients"].get("sdk", 0) >= 1 and isinstance(d["models"], list)
    assert body["agent_id"] not in str(d)
    summary = client.get("/relay/summary").json()
    assert summary["clients"].get("sdk", 0) >= 1


def test_registration_verifies_the_signed_card_and_refuses_a_tampered_one(client, monkeypatch):
    from emporia import operators
    from emporia.wire import relay_origin
    monkeypatch.setattr(rs, "REQUIRE_CHALLENGE", True)
    name = f"card_{uuid.uuid4().hex[:8]}"
    _, pub = identity.generate_or_load_keypair(name)
    relay = relay_origin(rs.RELAY_BASE_URL)
    label = operators.new_label()

    def attempt(signed_label, sent_label, client_name="sdk"):
        ch = client.post("/agents/challenge").json()
        sig = identity.sign(wire.signed_registration_payload(ch["nonce"], relay, name, pub.hex(), signed_label,
                                                              client_name, "0.2.0"), name)
        return client.post("/agents/register", json={
            "agent_id": name, "public_key_hex": pub.hex(), "challenge_id": ch["challenge_id"],
            "challenge_signature": sig, "relay": relay, "operator_label": sent_label, "client_name": client_name,
            "client_version": "0.2.0"})
    assert attempt(label, operators.new_label()).status_code == 403          # the card changed on the way
    assert attempt(label, label, "sdk").status_code == 200
    got = client.get(f"/agents/{name}", headers=_hdr(name)).json()
    assert got["operator_label"] == label and got["client"]["name"] == "sdk"


def test_a_line_sent_with_a_move_carries_the_declared_model_tag(client):
    sid, p = _chess_game(client)
    with rs.DB_LOCK, rs.get_db() as conn:
        s = rs.load_session(sid)
        conn.execute("UPDATE sessions SET config_json = ? WHERE session_id = ?",
                     (__import__("json").dumps({**s["config"], "chat": True}), sid))
    step = client.get(f"/sessions/{sid}", headers=_hdr(p["w"])).json()["step_number"]
    tags = [["model", "nous/hermes-4"]]
    payload = wire.signed_action_payload(stable_of(p["w"]), "move", {"uci": "e2e4"}, "Opening with the king pawn",
                                         sid, step, say="good luck", say_tags=tags)
    body = wire.action_envelope(p["w"], "move", {"uci": "e2e4"}, "Opening with the king pawn",
                                identity.sign(payload, p["w"]), say="good luck", say_tags=tags)
    assert client.post(f"/sessions/{sid}/action", json=body).status_code == 200
    talk = client.get(f"/sessions/{sid}/chat", headers=_hdr(p["w"])).json()["messages"]
    assert talk[0]["content"] == "good luck" and talk[0]["tags"] == tags
    # the tags are signed with the move: changing them in flight fails
    step = client.get(f"/sessions/{sid}", headers=_hdr(p["b"])).json()["step_number"]
    payload = wire.signed_action_payload(stable_of(p["b"]), "move", {"uci": "e7e5"}, "Answering in the centre",
                                         sid, step, say="you too", say_tags=tags)
    body = wire.action_envelope(p["b"], "move", {"uci": "e7e5"}, "Answering in the centre",
                                identity.sign(payload, p["b"]), say="you too", say_tags=[["model", "forged"]])
    assert client.post(f"/sessions/{sid}/action", json=body).status_code == 403


def test_the_mcp_move_sends_the_model_tag_with_its_line_unless_declaring_is_off(monkeypatch):
    from emporia import mcp_server as m
    sent = []

    class R:
        def __init__(self, code, data):
            self.status_code, self._d, self.text = code, data, ""

        def json(self):
            return self._d

        def raise_for_status(self):
            pass

    def fake(method, url, **kw):
        if method == "post":
            sent.append(kw["json"])
            return R(200, {"success": True})
        return R(200, {"step_number": 0, "module_type": "emporia:chess:v1", "sealed": False})
    monkeypatch.setattr(m, "_bearer_request", fake)
    monkeypatch.setattr(m, "AGENT_ID", "say_agent")
    identity.get_public_key_hex("say_agent")
    monkeypatch.setenv("EMPORIA_MODEL", "nous/hermes-4")
    monkeypatch.delenv("EMPORIA_DECLARE_MODEL", raising=False)
    monkeypatch.delenv("EMPORIA_DOTENV", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    m.submit_action("sess_x", "move", {"uci": "e2e4"}, "Opening with the king pawn", say="hello")
    assert sent[-1]["say_tags"] == [["model", "nous/hermes-4"]] and sent[-1]["model_commit"]
    monkeypatch.setenv("EMPORIA_DECLARE_MODEL", "off")
    m.submit_action("sess_x", "move", {"uci": "e2e4"}, "Opening with the king pawn", say="hello")
    assert "say_tags" not in sent[-1] and "model_commit" not in sent[-1]
