"""Review follow-up M4 (11-05): a dashboard sign-in names the relay it is for.

The v1 sign-in signed the relay's nonce alone (`emporia:challenge:v1\\n` + nonce), so a
signature taken for one relay's nonce was good wherever that nonce was issued, and
`sign_dashboard_challenge` without `relay_url` handed the raw signature to the model,
for whoever asked to talk it out. Now the agent signs `emporia:dashboard-sign-in:v2`
`{type, nonce, relay}`, the relay accepts only its own origin, and the tool completes
the sign-in itself, returning neither signature nor token."""
import json
from unittest.mock import patch

import pytest

from emporia import identity, signing, wire
from tests import test_emporia
from tests.test_emporia import _ASGIHttpxAdapter, _register_keyed

prod_client = test_emporia.prod_client      # the fixture; bound, not imported, so no F811


def _challenge(client) -> dict:
    return client.post("/dashboard/challenge").json()


def test_a_sign_in_is_good_only_on_the_relay_it_names(prod_client):
    _register_keyed(prod_client, "m4_named")
    elsewhere = signing.dashboard_session_body("m4_named", _challenge(prod_client), "https://other-relay.example")
    r = prod_client.post("/dashboard/session", json=elsewhere)
    assert r.status_code == 403 and "not this relay" in r.json()["detail"], r.text

    here = signing.dashboard_session_body("m4_named", _challenge(prod_client), "http://localhost:8088/")
    assert prod_client.post("/dashboard/session", json=here).status_code == 200

    # the relay named in the body must be the one signed: swapping it breaks the signature
    ch = _challenge(prod_client)
    signed_for_other = signing.dashboard_session_body("m4_named", ch, "https://other-relay.example")
    r = prod_client.post("/dashboard/session", json={**signed_for_other, "relay": "http://localhost:8088"})
    assert r.status_code == 403, r.text


def test_the_v1_sign_in_over_the_bare_nonce_is_refused_by_name(prod_client):
    _register_keyed(prod_client, "m4_v1")
    ch = _challenge(prod_client)
    r = prod_client.post("/dashboard/session", json={
        "agent_id": "m4_v1", "challenge_id": ch["challenge_id"],
        "signature_hex": "ab" * 64})
    assert r.status_code == 400 and "v1" in r.json()["detail"] and "dashboard-sign-in:v2" in r.json()["detail"]


def test_the_relay_takes_only_its_own_names_never_the_requests_host(prod_client, monkeypatch):
    from emporia import relay_server as rs
    monkeypatch.setenv("EMPORIA_RELAY_SIGN_IN_ORIGINS", "")
    assert rs._names_this_relay("http://127.0.0.1:8088")            # loopback spellings are one host
    assert not rs._names_this_relay("http://testserver")
    assert not rs._names_this_relay("not a url")
    monkeypatch.setenv("EMPORIA_RELAY_SIGN_IN_ORIGINS", "https://relay.example, http://testserver")
    assert rs._names_this_relay("https://relay.example/") and rs._names_this_relay("http://testserver")


def test_the_sign_in_tool_completes_it_and_returns_no_signature_or_token(prod_client, monkeypatch):
    from emporia import mcp_server
    _register_keyed(prod_client, "m4_tool")
    monkeypatch.setattr(mcp_server, "AGENT_ID", "m4_tool")
    monkeypatch.setattr(mcp_server, "_ask_operator", lambda *a, **k: True)
    ch = _challenge(prod_client)
    adapter = _ASGIHttpxAdapter(prod_client, mcp_server.RELAY_URL)
    with patch.object(mcp_server, "httpx", adapter):
        out = mcp_server.sign_dashboard_challenge(ch["challenge_id"], ch["nonce"])   # relay_url omitted
    assert out["success"] is True, out
    said = json.dumps(out)
    assert not {"signature", "signature_hex", "token"} & set(out), out
    polled = prod_client.get("/dashboard/poll", params={"challenge_id": ch["challenge_id"]}).json()
    assert polled["ready"] and polled["token"] not in said, "the dashboard picks it up; the model never sees it"


def test_the_sign_in_tool_refuses_a_nonce_before_asking_anyone(monkeypatch):
    from emporia import mcp_server
    monkeypatch.setattr(mcp_server, "AGENT_ID", "m4_tool2")
    monkeypatch.setattr(mcp_server, "_ask_operator", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    out = mcp_server.sign_dashboard_challenge("ch_x", '{"content":"x"}')
    assert out["success"] is False and "hex nonce" in out["error"]


@pytest.mark.parametrize("nonce, relay", [('{"content":"x"}', "https://relay.example"),
                                          ("ab" * 32, "not a relay"), ("ab" * 32, "ftp://relay.example")])
def test_the_signing_rule_signs_only_a_hex_nonce_for_a_relay_origin(nonce, relay):
    with pytest.raises(signing.Refused, match="not signed"):
        signing.sign("m4_rule", "dashboard-sign-in", nonce, relay)


def test_the_payload_is_its_own_kind():
    p = wire.signed_dashboard_sign_in_payload("ab" * 32, "HTTPS://Relay.Example:8443/some/path")
    assert p == {"type": "emporia:dashboard-sign-in:v2", "nonce": "ab" * 32, "relay": "https://relay.example:8443"}
    assert signing.KINDS["dashboard-sign-in"].builder == "signed_dashboard_sign_in_payload"
