"""Emporia 2.1 and 2.0.1 (the catalog release) talk to each other.

The 2.0.1 side is rebuilt from its code at be98e561, by hand (a literal card, a literal request body, a literal relay
card), never from the 2.1 builders, so a change to those cannot quietly change what the old side is assumed to send.
What is refused on purpose has its own tests (a v2 card that names a home, a request signed the old way)."""
from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from emporia import identity, wire
from emporia import mcp_server as m
from emporia import relay_server as rs
from emporia.relay_server import app

ORIGIN = "http://testserver"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51299))


def _v2_card(nonce, agent, pub, *, operator="", client=("hermes-plugin", "2.0.1"), about=""):
    """The registration card exactly as 2.0.1's `wire.signed_registration_payload` builds it."""
    return {"type": "emporia:sign-in:v2", "nonce": nonce, "relay": ORIGIN, "agent_id": agent,
            "public_key": pub.lower(), "operator": operator,
            "client": {"name": client[0], "version": client[1]}, "about": about}


def _register_as_201(client, agent, **card_kw):
    """A 2.0.1 `register_agent`: challenge, sign the v2 card, post the body 2.0.1 builds (no `home`)."""
    _, pub = identity.generate_or_load_keypair(agent)
    ch = client.post("/agents/challenge").json()
    sig = identity.sign(_v2_card(ch["nonce"], agent, pub.hex(), **card_kw), agent)
    body = {"agent_id": agent, "public_key_hex": pub.hex(), "display_name": agent, "challenge_id": ch["challenge_id"],
            "challenge_signature": sig, "relay": ORIGIN, "client_name": "hermes-plugin", "client_version": "2.0.1"}
    return client.post("/agents/register", json=body), pub


# ── a 2.0.1 plugin against a 2.1 relay ────────────────────────────────────────────────────────

def test_a_new_201_agent_registers_signs_in_and_acts_on_a_21_relay(client):
    agent = f"c201_{uuid.uuid4().hex[:8]}"
    r, _ = _register_as_201(client, agent)
    assert r.status_code == 200, r.text
    out = r.json()
    for key in ("status", "agent_id", "stable_id", "address", "fingerprint", "display_name", "operator_label",
                "about", "client"):                     # the fields 2.0.1's register answer carried
        assert key in out
    # its home is what 2.0.1 meant: this relay
    prof = client.get(f"/agents/{agent}/profile", headers={"x-emporia-agent-id": agent})
    if prof.status_code == 200:
        assert (prof.json().get("presence") or {}).get("standing", "member") == "member"
    # and the sign-in 2.0.1 does after registering is unchanged (dashboard-sign-in:v2)
    ch = client.post("/dashboard/challenge", json={"agent_id": agent}).json()
    sig = identity.sign({"type": "emporia:dashboard-sign-in:v2", "nonce": ch["nonce"], "relay": ORIGIN}, agent)
    s = client.post("/dashboard/session", json={"agent_id": agent, "challenge_id": ch["challenge_id"],
                                                "relay": ORIGIN, "signature": sig})
    assert s.status_code == 200 and s.json().get("token")


def test_a_201_card_with_a_label_a_bio_and_a_client_registers(client):
    agent = f"c201_{uuid.uuid4().hex[:8]}"
    label = "op_" + "c" * 26
    _, pub = identity.generate_or_load_keypair(agent)
    ch = client.post("/agents/challenge").json()
    card = _v2_card(ch["nonce"], agent, pub.hex(), operator=label, about="a plain bio")
    r = client.post("/agents/register", json={
        "agent_id": agent, "public_key_hex": pub.hex(), "challenge_id": ch["challenge_id"],
        "challenge_signature": identity.sign(card, agent), "relay": ORIGIN, "operator_label": label,
        "about": "a plain bio", "client_name": "hermes-plugin", "client_version": "2.0.1"})
    assert r.status_code == 200, r.text
    assert r.json()["about"] == "a plain bio" and r.json()["operator_label"] == label


def test_a_201_card_that_changes_after_signing_still_fails(client):
    agent = f"c201_{uuid.uuid4().hex[:8]}"
    _, pub = identity.generate_or_load_keypair(agent)
    ch = client.post("/agents/challenge").json()
    sig = identity.sign(_v2_card(ch["nonce"], agent, pub.hex(), about=""), agent)
    r = client.post("/agents/register", json={
        "agent_id": agent, "public_key_hex": pub.hex(), "challenge_id": ch["challenge_id"],
        "challenge_signature": sig, "relay": ORIGIN, "client_name": "hermes-plugin", "client_version": "2.0.1",
        "about": "added by a relay on the way"})
    assert r.status_code == 403


def test_a_201_card_cannot_be_replayed(client):
    agent = f"c201_{uuid.uuid4().hex[:8]}"
    _, pub = identity.generate_or_load_keypair(agent)
    ch = client.post("/agents/challenge").json()
    sig = identity.sign(_v2_card(ch["nonce"], agent, pub.hex()), agent)
    body = {"agent_id": agent, "public_key_hex": pub.hex(), "challenge_id": ch["challenge_id"],
            "challenge_signature": sig, "relay": ORIGIN, "client_name": "hermes-plugin", "client_version": "2.0.1"}
    assert client.post("/agents/register", json=body).status_code == 200
    again = client.post("/agents/register", json=body)          # the nonce is spent
    assert again.status_code == 400


def test_the_health_answer_still_carries_the_field_a_201_doctor_reads(client):
    h = client.get("/health").json()
    assert isinstance(h.get("starter_limits"), bool)             # 2.0.1's doctor reads `starter_limits`
    assert "allowance" in h


# ── a 2.1 plugin against a 2.0.1 relay (stubbed: its card says protocol 3 and it has no 2.1 routes) ─────

class _Old:
    """A 2.0.1 relay as the 2.1 plugin sees it over httpx: a protocol 3 card, the 2.0.1 routes it needs here, and
    the framework's bare 404 for every route that arrived in 2.1."""

    def __init__(self):
        self.seen = []

    def __call__(self, method):
        def go(url, **kw):
            path = url.split("old.test", 1)[-1]
            req = httpx.Request(method.upper(), url)
            self.seen.append((method, path, kw.get("json")))
            if path == "/relay/card":
                return httpx.Response(200, json={"kind": "emporia-relay-card", "version": 3}, request=req)
            if path == "/agents/challenge":
                return httpx.Response(200, json={"challenge_id": "c1", "nonce": "ab" * 32}, request=req)
            if path == "/agents/register":
                return httpx.Response(200, json={"status": "registered", "agent_id": kw["json"]["agent_id"],
                                                 "stable_id": "s" * 64, "address": "a@old.test",
                                                 "fingerprint": "f", "display_name": "x", "operator_label": "",
                                                 "about": "", "client": {}}, request=req)
            if path == "/labs":
                return httpx.Response(200, json={"labs": [{"slug": "x", "repo": "o/r", "owner": "o"}]}, request=req)
            return httpx.Response(404, json={"detail": "Not Found"}, request=req)
        return go


@pytest.fixture
def old_relay(monkeypatch):
    stub = _Old()
    for meth in ("get", "post", "delete", "put"):
        monkeypatch.setattr(httpx, meth, stub(meth))
    monkeypatch.setenv("EMPORIA_RELAY_URL", "http://old.test")
    monkeypatch.setattr(m, "RELAY_URL", "http://old.test")
    monkeypatch.setattr(m, "AGENT_ID", "c21_on_old")
    monkeypatch.setattr(m, "_dashboard_bearer_headers", lambda **kw: {})
    return stub


def test_a_21_plugin_registers_on_a_201_relay_with_the_old_card(old_relay):
    out = m.register_agent()
    assert out["success"] is True, out
    sent = next(j for meth, path, j in old_relay.seen if path == "/agents/register")
    assert "home" not in sent                                    # 2.0.1 has no homes to name
    _, pub = identity.generate_or_load_keypair("c21_on_old")
    card = {"type": "emporia:sign-in:v2", "nonce": "ab" * 32, "relay": "http://old.test", "agent_id": "c21_on_old",
            "public_key": pub.hex(), "operator": sent.get("operator_label", ""),
            "client": {"name": sent["client_name"], "version": sent["client_version"]},
            "about": sent.get("about", "")}
    assert identity.verify(card, sent["challenge_signature"], pub.hex())     # what 2.0.1 checks


@pytest.mark.parametrize("home, who", [("https://home.example", "visitor"), ("none", "nomad")])
def test_a_visit_or_nomad_is_not_quietly_made_a_plain_member_on_a_201_relay(monkeypatch, old_relay, home, who):
    monkeypatch.setenv("EMPORIA_HOME", home)
    out = m.register_agent()
    assert out["success"] is False and "previous Emporia release" in out["error"] and "your person" in out["error"]
    assert 'home="here"' not in out["error"], "the refusal tells the person, not the model, how to become a member"
    assert ("visitor" in out["error"]) == (who == "visitor") and ("nomad" in out["error"]) == (who == "nomad")
    assert not [1 for _, path, _ in old_relay.seen if path == "/agents/register"], "nothing was sent"
    # the agent's own `here` argument changes nothing: living there is its person's choice
    assert m.register_agent(home="here")["success"] is False
    assert not [1 for _, path, _ in old_relay.seen if path == "/agents/register"]
    # the person's other choice (emporia home NAME here): a plain member there, home here
    monkeypatch.setenv("EMPORIA_HOME", "here")
    out = m.register_agent()
    assert out["success"] is True, out
    assert "plain member" in out["note"]


def test_a_21_plugin_does_not_use_the_old_card_on_a_current_relay(monkeypatch, old_relay):
    monkeypatch.setattr(m, "_relay_protocol", lambda: wire.RELAY_PROTOCOL)
    m.register_agent()
    sent = next(j for meth, path, j in old_relay.seen if path == "/agents/register")
    _, pub = identity.generate_or_load_keypair("c21_on_old")
    legacy = wire.legacy_registration_payload("ab" * 32, "http://old.test", "c21_on_old", pub.hex(), "",
                                              sent["client_name"], sent["client_version"], "")
    assert not identity.verify(legacy, sent["challenge_signature"], pub.hex())


@pytest.mark.parametrize("call, what", [
    (lambda: m.lore(), "the Lore"),
])
def test_a_21_feature_on_a_201_relay_says_so_in_plain_words(old_relay, call, what):
    out = call()
    assert out["success"] is False
    assert what in out["error"] and "older Emporia" in out["error"] and "404" not in out["error"]


def test_the_21_tools_do_not_crash_on_a_201_relay(old_relay):
    for call in (lambda: m.whats_next(), lambda: m.standing(view="limits"), lambda: m.standing(view="stats"),
                 lambda: m.tesseract(), lambda: m.lab_threads(), lambda: m.my_settings(), lambda: m.my_quests(),
                 lambda: m.read_feed()):
        out = call()
        assert isinstance(out, dict)
        json.dumps(out, default=str)


def test_a_201_rate_limit_answer_still_gives_the_wait(monkeypatch):
    """2.0.1 sends only the `Retry-After` header on a 429 (2.1 adds `retry_in_s` to the body)."""
    from emporia import mcp_surface
    mcp_surface.note_retry_after("7")
    assert mcp_surface._wait_s({"success": False, "error": "Relay 429", "detail": "Rate limit exceeded"}) == 7


# ── the 2.0.1 settings a relay operator may still have ────────────────────────────────────────────

def test_the_201_starter_limits_switch_still_switches_the_allowance_off(monkeypatch):
    from emporia import allowance
    monkeypatch.delenv("EMPORIA_ALLOWANCE", raising=False)
    monkeypatch.setenv("EMPORIA_STARTER_LIMITS", "0")
    assert allowance.on() is False


def test_relay_card_of_a_201_relay_is_read_by_21_code():
    card = {"kind": "emporia-relay-card", "version": 3}
    assert wire.card_protocol(card) == 3 and rs._old_protocol(card)
    assert wire.card_protocol({}) is None and rs._old_protocol({}) == ""
