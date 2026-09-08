"""Bounty endpoints (P72-4) — through the relay, not the rule module.

tests/test_bounties.py proves the rules. This proves the wiring: that the rules
are actually consulted, that identity comes from the database rather than the
request, and that the Sybil limit survives the gap between checking and writing.
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from emporia.relay_server import app
from tests.test_emporia import _register


@pytest.fixture
def client():
    # Same shape as test_emporia's fixture: a loopback client so
    # EMPORIA_TRUST_LOCAL_HEADER_AUTH lets X-Emporia-Agent-Id stand in for the
    # bearer handshake. Redefined rather than imported — it lives in
    # test_emporia.py, not conftest, so it does not cross files.
    return TestClient(app, client=("127.0.0.1", 51216))


def _hdr(agent_id):
    return {"x-emporia-agent-id": agent_id}


def _mk_agent(client, *, nous_id=None, verified=True):
    """Register, then set trust/nous directly — registering with a real Nous JWT
    needs a live JWKS, and what is under test here is the entry rule, not the
    identity provider."""
    from emporia import relay_server as rs
    agent = "bty_" + uuid.uuid4().hex[:8]
    _register(client, agent)
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute(
            "UPDATE authorized_agents SET trust_level = ?, nous_user_id = ? WHERE agent_id = ?",
            ("nous_verified" if verified else "key_only", nous_id, agent),
        )
    return agent


def _post_bounty(client, funder, **kw):
    body = {
        "title": kw.get("title", "Beat me at chess"),
        "description": "A funded contest.",
        "module_type": kw.get("module_type", "emporia:chess:v1"),
        "prize_cents": kw.get("prize_cents", 5000),
        "entry_deadline": kw.get(
            "entry_deadline", (datetime.now(UTC) + timedelta(days=1)).isoformat()
        ),
    }
    return client.post("/bounties", json=body, headers=_hdr(funder))


def test_a_bounty_is_posted_discovered_and_entered(client):
    funder = _mk_agent(client, nous_id="nous_funder_" + uuid.uuid4().hex[:6])
    entrant = _mk_agent(client, nous_id="nous_entrant_" + uuid.uuid4().hex[:6])

    r = _post_bounty(client, funder)
    assert r.status_code == 200, r.text
    listing_id = r.json()["listing_id"]

    board = client.get("/bounties").json()
    mine = next(b for b in board["bounties"] if b["listing_id"] == listing_id)
    assert mine["prize_cents"] == 5000
    assert mine["entrant_count"] == 0
    assert mine["funder_id"] == funder

    assert client.post(f"/bounties/{listing_id}/enter",
                       headers=_hdr(entrant)).status_code == 200
    again = client.get("/bounties").json()
    assert next(b for b in again["bounties"]
                if b["listing_id"] == listing_id)["entrant_count"] == 1


def test_the_funder_cannot_enter_their_own_bounty(client):
    funder = _mk_agent(client, nous_id="nous_f_" + uuid.uuid4().hex[:6])
    listing_id = _post_bounty(client, funder).json()["listing_id"]
    r = client.post(f"/bounties/{listing_id}/enter", headers=_hdr(funder))
    assert r.status_code == 403 and "own prize" in r.json()["detail"]


def test_nor_can_the_funders_second_persona(client):
    """The wash trade an agent_id check alone misses. The funder's Nous account
    is pinned into the bounty at creation, so this holds even though the second
    persona is a different agent with a different key."""
    shared = "nous_shared_" + uuid.uuid4().hex[:6]
    funder = _mk_agent(client, nous_id=shared)
    other_face = _mk_agent(client, nous_id=shared)
    listing_id = _post_bounty(client, funder).json()["listing_id"]
    r = client.post(f"/bounties/{listing_id}/enter", headers=_hdr(other_face))
    assert r.status_code == 403 and "shares a Nous account" in r.json()["detail"]


def test_one_nous_account_gets_one_entry_through_the_api(client):
    shared = "nous_dup_" + uuid.uuid4().hex[:6]
    funder = _mk_agent(client, nous_id="nous_f_" + uuid.uuid4().hex[:6])
    a = _mk_agent(client, nous_id=shared)
    b = _mk_agent(client, nous_id=shared)
    listing_id = _post_bounty(client, funder).json()["listing_id"]
    assert client.post(f"/bounties/{listing_id}/enter", headers=_hdr(a)).status_code == 200
    r = client.post(f"/bounties/{listing_id}/enter", headers=_hdr(b))
    assert r.status_code == 403 and "one per account" in r.json()["detail"]


def test_the_unique_index_holds_when_the_check_is_bypassed(client):
    """The check reads, then the insert writes. Two personas of one account
    entering concurrently both pass the read — so the database, not the check,
    is what makes the rule true. Simulated by inserting directly, which is what
    a lost race looks like from the second writer's side.
    """
    import sqlite3

    import pytest

    from emporia import relay_server as rs
    shared = "nous_race_" + uuid.uuid4().hex[:6]
    funder = _mk_agent(client, nous_id="nous_f_" + uuid.uuid4().hex[:6])
    a = _mk_agent(client, nous_id=shared)
    b = _mk_agent(client, nous_id=shared)
    listing_id = _post_bounty(client, funder).json()["listing_id"]
    assert client.post(f"/bounties/{listing_id}/enter", headers=_hdr(a)).status_code == 200

    now = datetime.now(UTC).isoformat()
    with rs.DB_LOCK, rs.get_db() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO bounty_entries (listing_id, agent_id, nous_user_id, entered_at) "
                "VALUES (?,?,?,?)", (listing_id, b, shared, now),
            )


def test_a_key_only_agent_is_not_an_entrant(client):
    funder = _mk_agent(client, nous_id="nous_f_" + uuid.uuid4().hex[:6])
    stranger = _mk_agent(client, nous_id=None, verified=False)
    listing_id = _post_bounty(client, funder).json()["listing_id"]
    r = client.post(f"/bounties/{listing_id}/enter", headers=_hdr(stranger))
    assert r.status_code == 403 and "nous_verified" in r.json()["detail"]


def test_an_unsettleable_bounty_is_refused_at_creation(client):
    """Refused now, not after an entrant has done the work."""
    funder = _mk_agent(client, nous_id="nous_f_" + uuid.uuid4().hex[:6])
    assert _post_bounty(client, funder, prize_cents=1).status_code == 400
    assert _post_bounty(client, funder, module_type="emporia:research:v1").status_code == 400
    assert _post_bounty(
        client, funder,
        entry_deadline=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    ).status_code == 400
