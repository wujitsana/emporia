"""Cross-relay, first half: home / visitor / nomad in the signed card, the host that knows its visitors, EMPORIA_VISITORS,
visitors as other operators, and the passport card.

One relay runs in-process (the app under test is the host); the visitor's home is a faked relay answering
`GET /federation/v1/listed-agents`, signed like a real one."""
from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import e2e_tesseract as E

from emporia import identity, identity_card, operators, passport, signing, visitors, wire
from emporia import relay_server as rs
from emporia.relay_server import app

HOME = "http://home-relay.test"
HOME_ID = "rl_homerelay0001"
ORIGIN = "http://testserver"


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51290))


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("EMPORIA_VISITORS", raising=False)
    monkeypatch.setenv("EMPORIA_RELAY_SIGN_IN_ORIGINS", ORIGIN)
    visitors.forget()
    yield
    visitors.forget()


def _hdr(agent):
    return {"x-emporia-agent-id": agent}


def name(tag: str) -> str:
    return f"cv_{tag}_{uuid.uuid4().hex[:6]}"


def register(client, agent: str, home=None, *, label: str = "", card_type: str | None = None):
    """Register with a real signed card; `home` is what the card says (None: here). Returns the response."""
    _, pub = identity.generate_or_load_keypair(agent)
    ch = client.post("/agents/challenge").json()
    card = wire.signed_registration_payload(ch["nonce"], ORIGIN, agent, pub.hex(), label, "sdk", "0.2.0", "", home)
    if card_type:                                   # an old client's card
        card = {**wire.legacy_registration_payload(ch["nonce"], ORIGIN, agent, pub.hex(), label, "sdk", "0.2.0", "")}
    body = {"agent_id": agent, "public_key_hex": pub.hex(), "challenge_id": ch["challenge_id"],
            "challenge_signature": identity.sign(card, agent), "relay": ORIGIN, "operator_label": label,
            "client_name": "sdk", "client_version": "0.2.0"}
    if home is not None and wire.clean_home(home)["kind"] != "here":
        body["home"] = wire.clean_home(home)
    return client.post("/agents/register", json=body)


def visiting(url: str = HOME, relay_id: str = HOME_ID) -> dict:
    return {"kind": "relay", "relay_id": relay_id, "url": url}


def fake_home(monkeypatch, listed: list[str] | None, *, relay_id: str = HOME_ID, fail: bool = False):
    """The home relay: it lists these public keys as its own (signed), or does not answer."""
    real = httpx.get
    asked: list[str] = []

    def get(url, *a, **kw):
        if str(url).startswith(HOME):
            asked.append(str(url))
            if fail:
                raise httpx.ConnectTimeout("slow")
            agents = [{"agent_id": f"a{i}", "stable_id": "", "public_key_hex": k} for i, k in enumerate(listed or [])]
            body = {"origin_relay": HOME, "origin_public_key_hex": identity.get_relay_public_key_hex(),
                    "relay_id": relay_id,
                    "signature": identity.sign_raw_relay(identity.federation_signable_bytes(HOME, "listed_agents", agents)),
                    "agents": agents}
            return httpx.Response(200, json=body, request=httpx.Request("GET", url))
        return real(url, *a, **kw)
    monkeypatch.setattr(httpx, "get", get)
    visitors.forget()
    return asked


def presence(client, agent: str) -> dict:
    caller = name("viewer")
    register(client, caller)
    return client.get(f"/agents/{agent}", headers=_hdr(caller)).json()["presence"]


def pub_of(agent: str) -> str:
    return identity.generate_or_load_keypair(agent)[1].hex()


# ── registering as home, visitor and nomad ───────────────────────────────────────────────────────

def test_registering_at_home_is_a_member(client):
    a = name("home")
    assert register(client, a).status_code == 200
    p = presence(client, a)
    assert p["standing"] == "member" and p["home"]["kind"] == "here" and "label" not in p


def test_a_visitor_is_marked_with_its_home_and_the_home_confirms(client, monkeypatch):
    a = name("vis")
    fake_home(monkeypatch, [pub_of(a)])
    r = register(client, a, visiting())
    assert r.status_code == 200, r.text
    p = presence(client, a)          # the check ran after the answer (a background task, finished by now)
    assert p["standing"] == "visitor" and p["home"] == {"kind": "relay", "relay_id": HOME_ID, "url": HOME}
    assert p["check"]["status"] == "confirmed" and p["label"] == "visiting from home-relay.test"
    assert p["declared"] is True


def test_a_home_that_does_not_list_the_agent_or_does_not_answer_is_unconfirmed_and_blocks_nothing(client, monkeypatch):
    a, b, c = name("no"), name("slow"), name("other")
    fake_home(monkeypatch, ["ab" * 32])
    assert register(client, a, visiting()).status_code == 200
    assert presence(client, a)["check"]["status"] == "unconfirmed"
    assert "does not list" in presence(client, a)["check"]["reason"]
    assert presence(client, a)["label"] == "visiting from home-relay.test (unconfirmed)"
    fake_home(monkeypatch, None, fail=True)
    assert register(client, b, visiting()).status_code == 200            # registration is never held up by it
    got = presence(client, b)
    assert got["standing"] == "visitor" and got["check"]["status"] == "unconfirmed" and "did not answer" in got["check"]["reason"]
    fake_home(monkeypatch, [pub_of(c)], relay_id="rl_someoneelse01")     # that URL is another relay than the one named
    assert register(client, c, visiting()).status_code == 200
    assert "another relay" in presence(client, c)["check"]["reason"]


def test_a_nomad_has_no_home_and_nothing_to_check(client):
    a = name("nomad")
    assert register(client, a, "none").status_code == 200
    p = presence(client, a)
    assert p["standing"] == "nomad" and p["home"]["kind"] == "none" and p["label"] == "nomad" and "check" not in p


def test_the_card_and_the_directory_show_presence(client, monkeypatch):
    a = name("shown")
    fake_home(monkeypatch, [pub_of(a)])
    register(client, a, visiting())
    viewer = name("viewer")
    register(client, viewer)
    listing = client.get("/agents?limit=200", headers=_hdr(viewer)).json()["agents"]
    assert next(x for x in listing if x["agent_id"] == a)["presence"]["standing"] == "visitor"
    # the card is public once a relay list is published; the visitor publishes one naming its home
    _publish_list(client, a, HOME)
    card = client.get(f"/agents/{a}/card").json()["card"]
    assert card["presence"]["standing"] == "visitor" and card["relay_list"]["home"] == HOME


def _publish_list(client, agent: str, home: str, at: str | None = None):
    at = at or datetime.now(UTC).isoformat()
    stable = rs.AGENT_REGISTRY.get_stable_id(agent) or ""
    payload = wire.signed_relay_list_payload(stable, home, [], "", at)
    return client.post(f"/agents/{agent}/relay-list", headers=_hdr(agent), json={
        "home": home, "visits": [], "backups": [], "a2a_url": "", "updated_at": at,
        "signature": identity.sign(payload, agent)})


def test_a_visit_never_changes_home_a_member_is_not_registered_again_as_a_visitor(client, monkeypatch):
    a = name("stay")
    assert register(client, a).status_code == 200
    r = register(client, a, visiting())
    assert r.status_code == 409 and "lives here" in r.json()["detail"] and "move" in r.json()["detail"]
    assert presence(client, a)["standing"] == "member"


def test_the_signed_relay_list_moves_an_agent_to_nowhere_and_it_can_take_a_home_again(client, monkeypatch):
    a = name("move")
    assert register(client, a).status_code == 200
    assert _publish_list(client, a, "none").status_code == 200           # home -> nomad: its own signed list
    assert presence(client, a)["standing"] == "nomad"
    assert register(client, a, "none").status_code == 200                # it may now register as a nomad here
    assert register(client, a).status_code == 200                        # nomad -> home: registering as here
    assert presence(client, a)["standing"] == "member"
    # home -> another relay (a move): its signed list names it, and this relay now sees a visitor
    later = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
    assert _publish_list(client, a, HOME, later).status_code == 200
    assert presence(client, a)["standing"] == "visitor"


def test_an_old_card_is_refused_with_the_plugin_to_update(client):
    a = name("old")
    r = register(client, a, card_type="v2")
    assert r.status_code == 426
    assert "emporia:sign-in:v2" in r.json()["detail"] and "update the Emporia plugin" in r.json()["detail"]
    assert client.get(f"/agents/{a}/available").json().get("available", True)      # nothing was registered


def test_a_card_that_lies_about_home_does_not_verify(client):
    a = name("lie")
    _, pub = identity.generate_or_load_keypair(a)
    ch = client.post("/agents/challenge").json()
    card = wire.signed_registration_payload(ch["nonce"], ORIGIN, a, pub.hex(), "", "sdk", "0.2.0", "", "here")
    r = client.post("/agents/register", json={
        "agent_id": a, "public_key_hex": pub.hex(), "challenge_id": ch["challenge_id"],
        "challenge_signature": identity.sign(card, a), "relay": ORIGIN, "client_name": "sdk",
        "client_version": "0.2.0", "home": wire.clean_home("none")})      # a relay or proxy changing it on the way
    assert r.status_code == 403


def test_a_malformed_home_is_refused():
    for bad in ({"kind": "relay"}, {"kind": "relay", "url": "ftp://x"}, {"kind": "elsewhere"},
                {"kind": "none", "url": HOME}, "nowhere", {"kind": "relay", "url": HOME, "extra": 1}):
        with pytest.raises(ValueError):
            wire.clean_home(bad)
    assert wire.clean_home({"kind": "relay", "url": "HTTP://Home-Relay.test/x"})["url"] == HOME
    assert wire.registration_problem("ab" * 16, ORIGIN, "alice", "ab" * 32, "", None, "", {"kind": "relay"})


# ── the client: what it signs is its person's choice ──────────────────────────────────────────────

def test_the_client_signs_the_home_its_person_chose(monkeypatch):
    monkeypatch.delenv("EMPORIA_HOME", raising=False)
    mine = "https://mine.example"
    assert identity_card.home_card(mine, mine)["kind"] == "here"                       # registering at home
    got = identity_card.home_card("https://host.example", mine, "rl_x")
    assert got == {"kind": "relay", "relay_id": "rl_x", "url": mine}                  # a visit names home
    assert identity_card.home_card("https://host.example", mine, override="none")["kind"] == "none"
    monkeypatch.setenv("EMPORIA_HOME", "none")
    assert identity_card.home_card(mine, mine)["kind"] == "none" and identity_card.home_url(mine) == ""
    monkeypatch.setenv("EMPORIA_HOME", "https://elsewhere.example/")
    assert identity_card.home_url(mine) == "https://elsewhere.example"
    assert identity_card.home_card(mine, mine)["url"] == "https://elsewhere.example"
    assert identity_card.home_card("https://elsewhere.example", mine)["kind"] == "here"


def test_an_agent_cannot_sign_a_home_its_person_did_not_choose(monkeypatch):
    agent = name("signer")
    _, pub = identity.generate_or_load_keypair(agent)
    monkeypatch.setenv("EMPORIA_RELAY_URL", "https://mine.example")
    monkeypatch.delenv("EMPORIA_HOME", raising=False)
    nonce = "ab" * 32
    with pytest.raises(signing.SigningRefused if hasattr(signing, "SigningRefused") else Exception) as e:
        signing.sign_in(agent, nonce, "https://host.example", pub.hex(), home={"kind": "none"})
    assert "nomad" in str(e.value)
    with pytest.raises(Exception) as e2:
        signing.sign_in(agent, nonce, "https://host.example", pub.hex(),
                        home={"kind": "relay", "url": "https://other.example", "relay_id": ""})
    assert "its home is its person's choice" in str(e2.value)
    ok = signing.sign_in(agent, nonce, "https://host.example", pub.hex(),
                         home={"kind": "relay", "url": "https://mine.example", "relay_id": ""})
    assert identity.verify(wire.signed_registration_payload(
        nonce, "https://host.example", agent, pub.hex(), home={"kind": "relay", "url": "https://mine.example"}), ok, pub.hex())
    monkeypatch.setenv("EMPORIA_HOME", "none")
    assert signing.sign_in(agent, nonce, "https://host.example", pub.hex(), home={"kind": "none"})


# ── EMPORIA_VISITORS ─────────────────────────────────────────────────────────────────────────────

def test_each_visitors_mode(client, monkeypatch):
    fake_home(monkeypatch, [])
    monkeypatch.setenv("EMPORIA_VISITORS", "welcome")
    assert register(client, name("w1"), visiting()).status_code == 200
    assert register(client, name("w2"), "none").status_code == 200

    monkeypatch.setenv("EMPORIA_VISITORS", "homed-only")
    assert register(client, name("h1"), visiting()).status_code == 200
    r = register(client, name("h2"), "none")
    assert r.status_code == 403 and "have a home relay" in r.json()["detail"] and "homed-only" in r.json()["detail"]
    assert register(client, name("h3")).status_code == 200                       # a member is never a visitor

    monkeypatch.setenv("EMPORIA_VISITORS", "read-only")
    for home in (visiting(), "none"):
        r = register(client, name("r"), home)
        assert r.status_code == 403 and "read only" in r.json()["detail"]
    assert register(client, name("r3")).status_code == 200

    monkeypatch.setenv("EMPORIA_VISITORS", "closed")
    r = register(client, name("c1"), visiting())
    assert r.status_code == 403 and "takes no visitors" in r.json()["detail"]
    assert register(client, name("c2")).status_code == 200


def test_a_member_key_registers_under_any_home(client, monkeypatch):
    fake_home(monkeypatch, [])
    a = name("member")
    monkeypatch.setenv("EMPORIA_VISITORS", "closed")
    monkeypatch.setenv("EMPORIA_RELAY_MEMBERS", pub_of(a))
    assert register(client, a, visiting()).status_code == 200


def test_a_registered_visitor_cannot_write_when_the_operator_closes_the_door(client, monkeypatch):
    fake_home(monkeypatch, [])
    v, m, n = name("v"), name("m"), name("n")
    register(client, v, visiting())
    register(client, m)
    register(client, n, "none")
    post = lambda who: client.post(f"/agents/{who}/relay-list", headers=_hdr(who), json={})
    for mode, refused in (("welcome", ()), ("homed-only", (v, n)), ("read-only", (v, n)), ("closed", (v, n))):
        monkeypatch.setenv("EMPORIA_VISITORS", mode)
        for who in (v, m, n):
            r = post(who)
            assert (r.status_code == 403 and "EMPORIA_VISITORS" in r.text) == (who in refused), (mode, who, r.text)
    monkeypatch.setenv("EMPORIA_VISITORS", "closed")        # leaving always works
    assert client.post(f"/agents/{v}/leave", headers=_hdr(v)).status_code != 403


def test_the_relay_says_what_it_does_with_visitors(client, monkeypatch):
    assert client.get("/health").json()["visitors"] == "welcome"
    monkeypatch.setenv("EMPORIA_VISITORS", "homed-only")
    assert client.get("/health").json()["visitors"] == "homed-only"
    card = client.get("/relay/card").json()
    assert card["visitors"] == "homed-only" and identity.verify_relay_card(card)["ok"]
    monkeypatch.setenv("EMPORIA_VISITORS", "typo")
    assert visitors.mode() == "closed"                       # a typo closes, never opens


# ── the home's public list ───────────────────────────────────────────────────────────────────────

def test_a_relay_lists_the_agents_that_call_it_home_and_published_a_list(client):
    a, b, v = name("listed"), name("unlisted"), name("visitor")
    for who in (a, b):
        register(client, who)
    register(client, v, visiting())
    assert _publish_list(client, a, rs.RELAY_BASE_URL).status_code == 200
    assert _publish_list(client, v, HOME).status_code == 200            # a visitor's list names its own home
    body = client.get("/federation/v1/listed-agents").json()
    keys = {x["public_key_hex"] for x in body["agents"]}
    assert pub_of(a) in keys and pub_of(b) not in keys and pub_of(v) not in keys
    assert identity.verify_raw(identity.federation_signable_bytes(body["origin_relay"], "listed_agents", body["agents"]),
                               body["signature"], body["origin_public_key_hex"])
    assert body["relay_id"] == rs.relay_id()


# ── visitors count as other operators ────────────────────────────────────────────────────────────

@pytest.fixture
def w():
    world = E.World(TestClient(rs.app, client=("127.0.0.1", 51791)))
    yield world
    world.close()


def mark_visitor(w, agent: str) -> None:
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET home_json = ? WHERE agent_id = ?",
                     (json.dumps(visiting(), sort_keys=True), agent))
        conn.commit()
    w.rs.forget_who()


def test_a_visitor_fills_a_tesseract_table_and_the_operator_dedupe_still_holds(w):
    tag = uuid.uuid4().hex[:6]
    a = w.register(f"cv{tag}_a")
    b = w.register(f"cv{tag}_b")
    v = w.register(f"cv{tag}_v")                                  # its own operator, visiting
    twin = w.register(f"cv{tag}_twin", operator=a)               # a visitor of the same operator as a
    for who in (v, twin):
        mark_visitor(w, who)
    with rs.DB_LOCK, rs.get_db() as conn:
        ops = operators.of_agents(conn)
    assert len({ops[x] for x in (a, b, v)}) == 3                  # the visitor is one more operator
    assert ops[twin] == ops[a]                                    # and the same operator is still one
    sid = w.create(a, players=3)
    r = w.join(sid, twin, 1)
    assert r.status_code == 409 and "one seat per operator" in r.text
    assert w.join(sid, v, 1).status_code == 200
    assert w.join(sid, b, 2).status_code == 200
    assert w.state(sid)["status"] == "active" and v in w.state(sid)["participants"]


# ── the passport card ────────────────────────────────────────────────────────────────────────────

OTHER = "https://relay-a.example"


def carried(counted: int = 4) -> dict:
    return {"relay": OTHER, "relay_public_key": "cd" * 32, "signature": "sig", "scope": "this_relay",
            "label": f"history from {OTHER}", "counts_here": False, "counted": counted,
            "games": {"finished": counted, "won": 2, "lost": 2}, "as_of": "2026-11-01T00:00:00+00:00"}


def test_the_passport_has_identity_statements_and_an_unweighted_reach(client):
    a = name("pp")
    register(client, a, "none")
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET carried_records_json = ? WHERE agent_id = ?",
                     (json.dumps([carried()]), a))
        conn.commit()
    card = client.get(f"/agents/{a}/passport").json()
    assert card["kind"] == "emporia-passport" and card["counts_here"] is False
    ident = card["identity"]
    assert ident["agent_id"] == a and ident["public_key_hex"] == pub_of(a) and ident["fingerprint"]
    assert ident["address"].startswith(f"{a}@") and ident["presence"]["standing"] == "nomad"
    own, other = card["statements"]
    assert own["relay"] == rs.RELAY_BASE_URL and own["counts_here"] is True and own["checked"] == "signed by this relay"
    assert other["relay"] == OTHER and other["counts_here"] is False and other["says"] == f"{OTHER} says"
    assert other["record"]["games"]["finished"] == 4
    assert card["reach"] == {"relays": 1, "listed": [OTHER], "weighted": False, "text": "records on 1 relay"}
    assert "counts for nothing" in card["note"]
    assert passport.check(card) == [{"relay": rs.RELAY_BASE_URL, "ok": True}, {"relay": OTHER, "ok": None}]
    assert client.get("/agents/nobody_here/passport").status_code == 404


def test_reach_counts_relays_with_a_record_and_counts_each_once():
    own = {"relay": "https://me.example", "games": {"finished": 3}, "counted": 3, "as_of": "t"}
    built = passport.build({"agent_id": "x"}, own, [carried(), carried(2), {**carried(0), "games": {}, "relay": "https://empty.example"}])
    assert [s["relay"] for s in built["statements"]] == ["https://me.example", OTHER, "https://empty.example"]
    assert built["reach"]["relays"] == 2 and built["reach"]["text"] == "records on 2 relays"
    assert built["reach"]["weighted"] is False and all(s["counts_here"] is False for s in built["statements"][1:])


def test_a_passport_counts_for_nothing_here(client):
    """A carried statement is evidence only: the agent's XP and level on this relay do not move with it."""
    a = name("zero")
    register(client, a)
    viewer = name("viewer")
    register(client, viewer)
    before = client.get(f"/agents/{a}/quests", headers=_hdr(viewer)).json() if client.get(
        f"/agents/{a}/quests", headers=_hdr(viewer)).status_code == 200 else None
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute("UPDATE authorized_agents SET carried_records_json = ? WHERE agent_id = ?",
                     (json.dumps([carried(500)]), a))
        conn.commit()
    after = client.get(f"/agents/{a}/quests", headers=_hdr(viewer)).json() if client.get(
        f"/agents/{a}/quests", headers=_hdr(viewer)).status_code == 200 else None
    assert before == after


def test_the_agent_card_tool_shows_the_passport(client, monkeypatch):
    from emporia import mcp_server
    a = name("tool")
    register(client, a)
    _publish_list(client, a, rs.RELAY_BASE_URL)
    routed = {m: getattr(client, m) for m in ("get",)}
    monkeypatch.setattr(mcp_server.fetch, "get", lambda url, **kw: routed["get"](str(url).replace(ORIGIN, "")))
    monkeypatch.setattr(mcp_server, "RELAY_URL", ORIGIN)
    out = mcp_server._passport_of(ORIGIN, a)
    assert out["kind"] == "emporia-passport" and out["checks"] == [{"relay": rs.RELAY_BASE_URL, "ok": True}]
    assert out["reach"]["weighted"] is False


def test_the_home_check_never_calls_a_private_address_and_tells_nothing_of_why(monkeypatch):
    """The home is declared by the agent: loopback, private, link-local, metadata and plain-http targets are not
    called, and a failed check says one thing whatever went wrong."""
    from emporia import fetch
    monkeypatch.setattr(fetch, "_PASSTHROUGH", False)
    called: list[str] = []
    monkeypatch.setattr(fetch, "get", lambda url, **kw: called.append(url))
    for url in ("https://127.0.0.1", "https://10.0.0.5:8080", "https://169.254.169.254", "https://192.168.1.1",
                "https://[::1]", "https://0.0.0.0", "http://8.8.8.8", "ftp://8.8.8.8"):
        visitors.forget()
        assert visitors._pull(url) == {"error": visitors._UNANSWERED}, url
    assert called == []
    assert visitors.target_problem("https://8.8.8.8") == ""
    got = visitors.confirm({"kind": "relay", "url": "https://10.0.0.5"}, "ab" * 32)
    assert got["status"] == "unconfirmed" and got["reason"] == visitors._UNANSWERED


def test_homed_only_lets_a_visitor_act_only_once_its_home_confirmed_it(client, monkeypatch):
    v = name("conf")
    fake_home(monkeypatch, [pub_of(v)])
    register(client, v, visiting())
    monkeypatch.setenv("EMPORIA_VISITORS", "homed-only")
    assert visitors.write_refusal("relay", False) and "has not confirmed" in visitors.write_refusal("relay", False)
    assert visitors.write_refusal("relay", True) == ""
    assert client.post(f"/agents/{v}/relay-list", headers=_hdr(v), json={}).status_code != 403
