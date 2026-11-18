"""`emporia-standing` v1: sign and verify, the bindings, the carried copy, and Reach counting only what verifies."""
import copy
import json
import time
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from emporia import identity, passport
from emporia import relay_server as rs
from emporia import standing_statement as ss

AGENT_KEY = "ab" * 32
OTHER_KEY = "cd" * 32
NOW = 1_790_000_000.0
RELAY_URL = "https://relay-a.example"


class FakeRelay:
    """A relay that is not this one: its own key, its own id."""

    def __init__(self, url=RELAY_URL):
        self.key = ed25519.Ed25519PrivateKey.generate()
        self.pub = self.key.public_key().public_bytes_raw().hex()
        self.id = identity.relay_id_from_key(self.pub)
        self.url = url

    def issuer(self):
        return {"relay_id": self.id, "relay_url": self.url, "relay_public_key": self.pub}

    def statement(self, *, counted=3, subject_key=AGENT_KEY, audience=None, now=NOW, days=7, facts=None):
        f = {"counted_operators": counted, "operator_weight_sum": float(counted), "renown": {"score": 1.5},
             "trust": {"score": 2.0}, "allowance": {"tier": "Known", "source": "earned"},
             "contact_units": [{"unit": ss.unit_token(self.id, f"op{i}"), "weight": 1.0} for i in range(counted)]}
        return ss.build(issuer=self.issuer(), subject={"agent_id": "x", "public_key_hex": subject_key},
                        facts={**f, **(facts or {})}, context={"operators_active_30d": 5},
                        sign=lambda b: self.key.sign(b).hex(), audience=audience, now=now, days=days)


def test_a_signed_statement_verifies_and_says_who_signed_it():
    relay = FakeRelay()
    st = relay.statement()
    got = ss.verify(st, subject_key=AGENT_KEY, now=NOW + 3600)
    assert got["ok"] and got["relay_id"] == relay.id and st["signature"]
    assert st["kind"] == "emporia-standing" and st["v"] == 1
    assert ss.signable(st).startswith(b"emporia-standing:v1:")


def test_a_statement_lasts_at_most_fourteen_days():
    relay = FakeRelay()
    long = relay.statement(days=90)                     # build clamps
    assert ss.verify(long, now=NOW)["ok"]
    assert ss._ts(long["expires_at"]) - ss._ts(long["issued_at"]) == 14 * 86_400
    forged = copy.deepcopy(relay.statement())
    forged["expires_at"] = ss._iso(NOW + 30 * 86_400)
    forged["signature"] = relay.key.sign(ss.signable(forged)).hex()      # signed, but over the limit
    assert "at most 14 days" in ss.verify(forged, now=NOW)["reason"]


@pytest.mark.parametrize("path,value", [
    (("facts", "counted_operators"), 40), (("facts", "trust", "score"), 99), (("issuer", "relay_url"), "https://evil.example"),
    (("subject", "agent_id"), "someone_else"), (("issued_at",), "2026-01-01T00:00:00+00:00"),
    (("context", "operators_active_30d"), 500),
])
def test_a_tampered_statement_fails(path, value):
    st = FakeRelay().statement()
    target = st
    for k in path[:-1]:
        target = target[k]
    target[path[-1]] = value
    got = ss.verify(st, now=NOW)
    assert not got["ok"] and "signature" in got["reason"]


def test_the_wrong_subject_key_fails():
    st = FakeRelay().statement()
    assert ss.verify(st, subject_key=AGENT_KEY, now=NOW)["ok"]
    got = ss.verify(st, subject_key=OTHER_KEY, now=NOW)
    assert not got["ok"] and "another key" in got["reason"]


def test_an_expired_or_future_statement_fails():
    relay = FakeRelay()
    st = relay.statement()
    late = ss.verify(st, now=NOW + 8 * 86_400)
    assert not late["ok"] and late["expired"] is True and "expired" in late["reason"]
    assert not ss.verify(st, now=NOW - 7200)["ok"]


def test_the_relay_id_must_come_from_the_issuers_key():
    relay = FakeRelay()
    st = relay.statement()
    st["issuer"] = {**st["issuer"], "relay_id": "aaaaaaaaaaaaaaaa"}
    st["signature"] = relay.key.sign(ss.signable(st)).hex()             # signed, but the id is not its key's
    assert "relay id" in ss.verify(st, now=NOW)["reason"]
    # a different key cannot sign as someone else's id
    other = FakeRelay()
    st2 = relay.statement()
    st2["issuer"]["relay_public_key"] = other.pub
    assert not ss.verify(st2, now=NOW)["ok"]


def test_a_pinned_key_must_match():
    relay = FakeRelay()
    assert ss.verify(relay.statement(), pinned_relay_key=relay.pub, now=NOW)["ok"]
    assert not ss.verify(relay.statement(), pinned_relay_key=FakeRelay().pub, now=NOW)["ok"]


def test_an_audience_bound_statement_is_for_that_relay_only():
    relay = FakeRelay()
    st = relay.statement(audience="hostrelayaaaaaaa")
    assert ss.verify(st, audience="hostrelayaaaaaaa", now=NOW)["ok"]
    assert "another relay" in ss.verify(st, audience="otherrelay000000", now=NOW)["reason"]
    assert not ss.verify(st, now=NOW)["ok"], "a reader that names no audience refuses an audience-bound copy"
    assert ss.verify(relay.statement(), audience="otherrelay000000", now=NOW)["ok"], "an open statement is for anyone"


def test_shape_is_checked_and_nothing_else_rides_along():
    relay = FakeRelay()
    st = relay.statement()
    for bad in ({**st, "note": "hi"}, {**st, "kind": "other"}, {**st, "v": 2}):
        assert not ss.verify(bad, now=NOW)["ok"]
    loud = relay.statement(facts={"text": "x" * 1000})
    assert "does not allow" in ss.verify(loud, now=NOW)["reason"]
    huge = relay.statement(facts={"blob": [{"a": "b" * 250}] * 40})
    assert not ss.verify(huge, now=NOW)["ok"]
    assert not ss.verify("not a dict")["ok"] and not ss.verify(None)["ok"]


def test_contact_units_are_opaque_and_scoped_to_the_issuer():
    a, b = FakeRelay(), FakeRelay()
    assert ss.unit_token(a.id, "label:op_x") == ss.unit_token(a.id, "label:op_x")
    assert ss.unit_token(a.id, "label:op_x") != ss.unit_token(b.id, "label:op_x")
    assert "op_x" not in ss.unit_token(a.id, "label:op_x")
    from emporia.chronicle_ledger import unit_token as chronicle_unit
    assert ss.unit_token(a.id, "k") != chronicle_unit(a.id, "k"), "a statement cannot be matched to a public ballot"


def test_shown_is_evidence_with_the_why_and_never_counted():
    relay = FakeRelay()
    st = relay.statement()
    seen = ss.shown(st, ss.verify(st, now=NOW))
    assert seen["verified"] and seen["counted_here"] is False
    assert "standing at https://relay-a.example" in seen["line"] and "verified: signed by relay" in seen["line"]
    assert "not counted here" in seen["line"] and "Renown 1.5" in seen["line"]
    bad = ss.shown(st, {"ok": False, "reason": "the statement has expired"})
    assert not bad["verified"] and "not verified (the statement has expired)" in bad["line"]


# ── Reach counts only what verifies and counted standing ─────────────────────────────────────────

def _carried(relay, st):
    return {"relay": relay.url, "relay_public_key": relay.pub, "signature": "", "label": "standing", "counts_here": False,
            "standing": st}


def test_reach_ignores_unverified_and_empty_statements():
    good, thin, forged, wrong = FakeRelay("https://g.example"), FakeRelay("https://t.example"), \
        FakeRelay("https://f.example"), FakeRelay("https://w.example")
    tampered = forged.statement()
    tampered["facts"]["counted_operators"] = 50
    built = passport.build(
        {"agent_id": "x", "public_key_hex": AGENT_KEY},
        {"relay": "https://me.example", "games": {"finished": 9}, "counted": 9, "as_of": "t"},
        [_carried(good, good.statement(now=time.time())), _carried(thin, thin.statement(counted=1, now=time.time())),
         _carried(forged, tampered), _carried(wrong, wrong.statement(subject_key=OTHER_KEY, now=time.time()))])
    assert built["reach"]["listed"] == ["https://g.example"] and built["reach"]["relays"] == 1
    by = {s["relay"]: s for s in built["statements"]}
    assert by["https://g.example"]["standing"]["verified"] is True
    assert by["https://t.example"]["standing"]["verified"] is True       # shown, counts nothing toward Reach
    assert by["https://f.example"]["standing"]["verified"] is False
    assert by["https://w.example"]["standing"]["verified"] is False
    assert all(s["counts_here"] is False for s in built["statements"][1:])
    assert passport.check(built)[1] == {"relay": "https://g.example", "ok": None, "standing_ok": True}
    assert [c.get("standing_ok") for c in passport.check(built)[2:]] == [True, False, False]


def test_a_bare_old_record_adds_nothing_to_reach():
    old = {"relay": "https://old.example", "relay_public_key": "cd" * 32, "signature": "sig", "counted": 4,
           "games": {"finished": 4}, "counts_here": False}
    built = passport.build({"agent_id": "x"}, {"relay": "https://me.example", "counted": 0, "as_of": "t"}, [old])
    assert built["reach"]["relays"] == 0 and built["statements"][1]["record"]["games"]["finished"] == 4


# ── the relay: issue, carry, show ────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EMPORIA_GAMIFY", "sheets")
    monkeypatch.setenv("EMPORIA_ALLOWANCE", "1")
    return TestClient(rs.app)


def _keyed(client, agent):
    from tests.test_quest_knobs import _register_keyed
    _register_keyed(client, agent)
    with rs.DB_LOCK, rs.get_db() as conn:
        return conn.execute("SELECT public_key_hex FROM authorized_agents WHERE agent_id = ?", (agent,)).fetchone()[0]


def test_the_relay_issues_a_statement_bound_to_the_agents_key_and_its_own_id(client):
    a = f"ss_{uuid.uuid4().hex[:6]}"
    key = _keyed(client, a)
    st = client.get(f"/agents/{a}/standing-statement").json()
    assert ss.verify(st, subject_key=key)["ok"]
    assert st["issuer"]["relay_id"] == rs.relay_id() and st["issuer"]["relay_public_key"] == identity.get_relay_public_key_hex()
    assert st["subject"]["public_key_hex"] == key and st["audience"] is None
    f = st["facts"]
    assert f["counted_operators"] == 0 and f["allowance"]["source"] == "earned" and f["contact_units"] == []
    assert "vouched" not in json.dumps(st)
    assert client.get("/agents/ss_nobody/standing-statement").status_code == 404
    assert client.get(f"/agents/{a}/standing-statement", params={"audience": "bad!"}).status_code == 400
    bound = client.get(f"/agents/{a}/standing-statement", params={"audience": "hostrelayaaaaaaa"}).json()
    assert bound["audience"] == "hostrelayaaaaaaa" and ss.verify(bound, audience="hostrelayaaaaaaa", subject_key=key)["ok"]


def test_a_vouched_tier_is_never_exported(client, monkeypatch):
    a = f"ss_{uuid.uuid4().hex[:6]}"
    _keyed(client, a)
    with rs.DB_LOCK, rs.get_db() as conn:
        stable = conn.execute("SELECT stable_id FROM authorized_agents WHERE agent_id = ?", (a,)).fetchone()[0]
    monkeypatch.setenv("EMPORIA_VOUCHED_STABLE_IDS", stable)
    sheet = client.get(f"/agents/{a}/sheet").json()
    st = client.get(f"/agents/{a}/standing-statement").json()
    if sheet.get("allowance", {}).get("source") == "vouched":     # the relay vouches here: the sheet says so, the statement does not
        assert st["facts"]["allowance"] == {"tier": "Newcomer", "source": "earned"}
    assert "vouched" not in json.dumps(st)


def test_the_statement_needs_the_sheets_module(client, monkeypatch):
    monkeypatch.delenv("EMPORIA_GAMIFY", raising=False)
    assert client.get("/agents/anyone/standing-statement").status_code == 404


def test_a_carried_statement_is_kept_whole_verifies_and_counts_for_nothing(client):
    from tests.test_emporia import bearer_for
    a = f"ss_{uuid.uuid4().hex[:6]}"
    key = _keyed(client, a)
    relay = FakeRelay()
    h = bearer_for(client, a)
    st = relay.statement(subject_key=key, now=time.time())
    r = client.post(f"/agents/{a}/carried-record", json={"standing": st}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["standing"]["counted_here"] is False
    card = client.get(f"/agents/{a}/passport").json()
    other = card["statements"][1]
    assert other["standing_statement"] == st, "kept whole, so any reader can verify it"
    assert other["standing"]["verified"] and "not counted here" in other["standing"]["line"]
    assert card["reach"]["relays"] == 1 and card["reach"]["listed"] == [relay.url]
    assert passport.check(card)[1]["standing_ok"] is True
    assert ss.verify(other["standing_statement"], subject_key=key, audience=card["host_relay_id"])["ok"]
    # counted nowhere here
    assert client.get(f"/agents/{a}/sheet").json()["allowance"]["tier"] == 1
    assert client.get(f"/agents/{a}/sheet").json()["levels"]["trust"] == 1


def test_a_carried_statement_for_another_key_or_tampered_or_expired_is_refused(client):
    from tests.test_emporia import bearer_for
    a = f"ss_{uuid.uuid4().hex[:6]}"
    key = _keyed(client, a)
    h = bearer_for(client, a)
    relay = FakeRelay()
    for bad, why in ((relay.statement(subject_key=OTHER_KEY, now=time.time()), "another key"),
                     (relay.statement(subject_key=key, now=time.time() - 20 * 86_400), "expired"),
                     (relay.statement(subject_key=key, now=time.time(), audience="otherrelay000000"), "another relay")):
        r = client.post(f"/agents/{a}/carried-record", json={"standing": bad}, headers=h)
        assert r.status_code == 422 and why in r.text, (why, r.text)
    tampered = relay.statement(subject_key=key, now=time.time())
    tampered["facts"]["counted_operators"] = 99
    assert client.post(f"/agents/{a}/carried-record", json={"standing": tampered}, headers=h).status_code == 422
    assert client.post(f"/agents/{a}/carried-record", json={}, headers=h).status_code == 422
    # another agent cannot carry it for this one
    b = f"ss_{uuid.uuid4().hex[:6]}"
    _keyed(client, b)
    ok = relay.statement(subject_key=key, now=time.time())
    assert client.post(f"/agents/{a}/carried-record", json={"standing": ok}, headers=bearer_for(client, b)).status_code == 403


def test_a_newer_statement_from_the_same_relay_replaces_the_old_one(client):
    from tests.test_emporia import bearer_for
    a = f"ss_{uuid.uuid4().hex[:6]}"
    key = _keyed(client, a)
    h = bearer_for(client, a)
    relay = FakeRelay()
    for counted in (3, 4):
        assert client.post(f"/agents/{a}/carried-record", json={"standing": relay.statement(
            subject_key=key, now=time.time(), counted=counted)}, headers=h).status_code == 200
    statements = client.get(f"/agents/{a}/passport").json()["statements"]
    assert len(statements) == 2 and statements[1]["standing_statement"]["facts"]["counted_operators"] == 4


def test_an_old_passport_without_statements_still_reads():
    """A 2.0.1 relay's passport carries no standing and no host id: a 2.1 reader must not need either."""
    old = {"kind": "emporia-passport", "statements": [
        {"relay": "https://old.example", "counts_here": True, "signature": "", "record": {}, "relay_public_key": ""}]}
    assert passport.check(old) == [{"relay": "https://old.example", "ok": False}]
