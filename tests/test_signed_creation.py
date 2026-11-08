"""P279: a game is made in its creator's own name — signed, naming the game by
the id its nonce gives — so a relay cannot make one in an agent's name, and its
players seat the creator by that signature rather than the relay's word."""
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from emporia import identity, wire
from emporia.relay_server import app
from tests.ids import stable_of
from tests.test_emporia import _publish_enc_key, _register_keyed

RELAY = "http://testserver"


def test_a_game_is_signed_by_its_creator_and_named_by_its_nonce():
    from conftest import header_shim_off, signed_session_body
    from tests.test_emporia import bearer_for
    with header_shim_off():
        c = TestClient(app, client=("127.0.0.1", 51279))
        _register_keyed(c, "sc_maker")
        _register_keyed(c, "sc_other")
        h = bearer_for(c, "sc_maker")
        body = {"module_type": "emporia:service:v1", "config": {"description": "work"}}
        assert c.post("/sessions", json=body, headers=h).status_code == 401
        assert c.post("/sessions", json=signed_session_body("sc_other", body), headers=h).status_code == 403
        late = signed_session_body("sc_maker", body) | {"module_type": "emporia:chess:v1"}   # not what was signed
        assert c.post("/sessions", json=late, headers=h).status_code == 403
        signed = signed_session_body("sc_maker", body)
        r = c.post("/sessions", json=signed, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["session_id"] == wire.session_id_for(signed["nonce"])
        assert c.post("/sessions", json=signed, headers=h).status_code == 409        # once
        stale = signed_session_body("sc_maker", body)
        stale["at"] = "2026-01-01T00:00:00+00:00"
        stale["signature"] = identity.sign(wire.signed_session_create_payload(
            wire.session_id_for(stale["nonce"]), "sc_maker", body["module_type"], stale["at"],
            wire.price_terms(None)), "sc_maker")
        assert c.post("/sessions", json=stale, headers=h).status_code == 400


def test_players_seat_the_creator_only_by_its_signed_creation():
    client = TestClient(app, client=("127.0.0.1", 51280))
    for a in ("scs_maker", "scs_joiner"):
        _register_keyed(client, a)
        _publish_enc_key(client, a)
    sid = client.post("/sessions", json={"module_type": "emporia:service:v1", "config": {"description": "w"}},
                      headers={"x-emporia-agent-id": "scs_maker"}).json()["session_id"]
    assert client.post(f"/sessions/{sid}/join", json={"agent_id": "scs_joiner"},
                       headers={"x-emporia-agent-id": "scs_joiner"}).status_code == 200
    served = client.get(f"/sessions/{sid}/encryption-keys", headers={"x-emporia-agent-id": "scs_joiner"}).json()
    seated = identity.seated_players(sid, served, "scs_joiner", "scs_joiner", RELAY)
    assert seated == {"scs_maker", "scs_joiner"}
    # A relay naming someone else as the maker, or another module, or dropping the signature: not seated.
    assert identity.seated_players(sid, {**served, "creator": "scs_joiner"}, "scs_maker", "scs_maker", RELAY) == set()
    assert "scs_maker" not in identity.seated_players(sid, {**served, "module_type": "emporia:chess:v1"},
                                                      "scs_joiner", "scs_joiner", RELAY)
    unsigned = {**served, "joins": {k: v for k, v in served["joins"].items() if k != "scs_maker"}}
    assert "scs_maker" not in identity.seated_players(sid, unsigned, "scs_joiner", "scs_joiner", RELAY)
    at = datetime.now(UTC).isoformat()
    as_join = {**served, "joins": {**served["joins"], "scs_maker": {"at": at, "signature": identity.sign(
        wire.signed_session_join_payload(sid, stable_of("scs_maker"), at), "scs_maker")}}}
    assert "scs_maker" not in identity.seated_players(sid, as_join, "scs_joiner", "scs_joiner", RELAY), \
        "a join is not a creation"


def test_the_creator_signs_the_stake_and_a_relay_that_raises_it_is_caught():
    """P280: the money terms the creator asked for are in what it signs; a player
    pays the signed stake, and a relay keeping another one is refused."""
    import json

    import pytest

    from emporia import relay_server as rs
    client = TestClient(app, client=("127.0.0.1", 51281))
    for a in ("st_maker", "st_joiner"):
        _register_keyed(client, a)
        _publish_enc_key(client, a)
    rules = {"mode": "x402", "price": "2.00", "currency": "USD"}
    sid = client.post("/sessions", json={"module_type": "emporia:service:v1", "config": {"description": "w"},
                                         "payment_rules": rules},
                      headers={"x-emporia-agent-id": "st_maker"}).json()["session_id"]
    assert client.post(f"/sessions/{sid}/join", json={"agent_id": "st_joiner"},
                       headers={"x-emporia-agent-id": "st_joiner"}).status_code == 200
    served = client.get(f"/sessions/{sid}/encryption-keys", headers={"x-emporia-agent-id": "st_joiner"}).json()
    assert served["terms"]["price"] == "2.00"
    assert identity.game_terms(sid, served, "st_joiner", "st_joiner", RELAY) == ({"st_maker"}, "2.00")

    with rs.get_db() as conn:   # the relay raises the stake it keeps
        row = conn.execute("SELECT payment_rules_json FROM sessions WHERE session_id = ?", (sid,)).fetchone()
        conn.execute("UPDATE sessions SET payment_rules_json = ? WHERE session_id = ?",
                     (json.dumps({**json.loads(row[0]), "price": "5.00"}), sid))
    raised = client.get(f"/sessions/{sid}/encryption-keys", headers={"x-emporia-agent-id": "st_joiner"}).json()
    with pytest.raises(ValueError, match="5.00, not the 2.00"):
        identity.game_terms(sid, raised, "st_joiner", "st_joiner", RELAY)
    # And its signed terms changed too: then the creation does not verify, and the game is refused
    # (review of P280: before, the stake fell back to the relay's word).
    forged = {**raised, "terms": {**raised["terms"], "price": "5.00"}}
    with pytest.raises(ValueError, match="no creation signed by st_maker"):
        identity.game_terms(sid, forged, "st_joiner", "st_joiner", RELAY)
    stripped = {**served, "joins": {k: v for k, v in served["joins"].items() if k != "st_maker"}}
    with pytest.raises(ValueError, match="no creation signed"):
        identity.game_terms(sid, stripped, "st_joiner", "st_joiner", RELAY)
    # A game whose stake the creator left to the module: nothing signed to hold it to.
    sid2 = client.post("/sessions", json={"module_type": "emporia:service:v1", "config": {"description": "w"}},
                       headers={"x-emporia-agent-id": "st_maker"}).json()["session_id"]
    client.post(f"/sessions/{sid2}/join", json={"agent_id": "st_joiner"}, headers={"x-emporia-agent-id": "st_joiner"})
    served2 = client.get(f"/sessions/{sid2}/encryption-keys", headers={"x-emporia-agent-id": "st_joiner"}).json()
    assert identity.game_terms(sid2, served2, "st_joiner", "st_joiner", RELAY) == ({"st_maker"}, None)
