"""The relay's own clock gates a randomness commitment (timing audit): a backdated signed `at` must not let a
commitment name a round that is already published, and a join cannot come after the game's round is out."""
import time
import uuid

from conftest import make_test_commit
from fastapi.testclient import TestClient

from emporia import rand, rand_client
from emporia import relay_server as rs
from emporia.relay_server import app
from tests.test_emporia import _register_keyed

LIARS = "emporia:liars-dice:v1"


def _on(monkeypatch):
    monkeypatch.setattr(rand, "RELAY_CLOCK_CHECK", True)
    monkeypatch.setattr(rand, "MIN_LEAD_ROUNDS", 10)


def test_the_relay_clock_refuses_a_backdated_commitment_and_a_join_after_the_round(monkeypatch):
    _on(monkeypatch)
    now = time.time()
    cur = rand.round_at(now)
    assert rand.relay_clock_problem(cur + 10, now) == ""
    assert "relay's clock" in rand.relay_clock_problem(cur - 5, now)
    assert rand.relay_clock_problem(cur + 40, now, cur + 12) == ""
    assert "already published" in rand.relay_clock_problem(cur + 40, now, cur - 1)


def test_an_honest_2_0_1_client_still_passes_with_a_normal_delivery_delay(monkeypatch):
    """N-1: a client names the signed time's round + CLIENT_LEAD_ROUNDS (30); the relay needs now's round + 10, so a
    request may take (30 - 10) rounds = 60 s to arrive."""
    _on(monkeypatch)
    t = 1_800_000_000.0
    c, _ = rand_client.make_commit(lambda *a: "sig", "sess_q", "me", 0, now=t)
    assert rand.commit_round_problem(c["at"], c["beacon_round"]) == ""
    for delay in (0, 2, 10, 30, 55):
        assert rand.relay_clock_problem(c["beacon_round"], t + delay) == "", delay
    assert rand.relay_clock_problem(c["beacon_round"], t + 75) != "", "held for over a minute: refused"


def test_the_routes_apply_the_relay_clock_to_create_join_and_dealt_weights(monkeypatch):
    c = TestClient(app, client=("127.0.0.1", 51240))
    a, b = f"rc_{uuid.uuid4().hex[:6]}", f"rd_{uuid.uuid4().hex[:6]}"
    _register_keyed(c, a)
    _register_keyed(c, b)
    h = {"x-emporia-agent-id": a}
    _on(monkeypatch)
    monkeypatch.setattr(rand, "commit_round_problem", lambda *_: "")    # `at` backdated: only the relay's clock is left
    # create: the shim's commitment names a published round
    r = c.post("/sessions", json={"module_type": LIARS}, headers=h)
    assert r.status_code == 400 and "relay's clock" in r.text, r.text
    # dealt Blotto: the weights round, the same way
    r = c.post("/sessions", json={"module_type": "emporia:blotto:v2",
                                  "config": {"dealt": True, "weights_round": rand.round_at(time.time()) - 5}}, headers=h)
    assert r.status_code == 400 and "weights_round" in r.text and "relay's clock" in r.text, r.text
    # join: a game whose round is already published refuses a fresh, well-led commitment
    monkeypatch.setattr(rand, "RELAY_CLOCK_CHECK", False)
    created = c.post("/sessions", json={"module_type": LIARS}, headers=h)
    assert created.status_code == 200, created.text
    sid = created.json()["session_id"]
    assert rs.load_session(sid)["config"]["rand"]["commits"][a]["beacon_round"] < rand.round_at(time.time())
    monkeypatch.setattr(rand, "RELAY_CLOCK_CHECK", True)
    r = c.post(f"/sessions/{sid}/join",
               json={"agent_id": b, "rand_commit": make_test_commit(sid, b, 1, round_offset=40)},
               headers={"x-emporia-agent-id": b})
    assert r.status_code == 400 and "already published" in r.text, r.text
