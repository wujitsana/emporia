"""Bounty endpoints (P72-4) — through the relay, not the rule module.

tests/test_bounties.py proves the rules. This proves the wiring: that the rules
are actually consulted, that identity comes from the database rather than the
request, and that the Sybil limit survives the gap between checking and writing.
"""
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from emporia.relay_server import app
from tests.test_emporia import _register, _register_keyed, _signed_action_body


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


@pytest.fixture(autouse=True)
def _escrow():
    """A fake Stripe, not a fake escrow (P77).

    The previous fixture patched `create_stake_intent` and returned a
    PaymentIntent id. That hid the actual defect: the real call created a PI
    with no payment method and no confirm, so it sat in
    `requires_payment_method` and held nothing — and a mock that hands back an
    id cannot tell a held prize from an unheld one.

    This mocks the Stripe HTTP boundary instead, so `confirm_spt()` →
    `verify_payment_intent()` → `_assert_payment_intent_matches()` all run for
    real. The PI it returns is `requires_capture` — authorised, not charged —
    with the metadata binding the relay checks. A route that stopped confirming
    would get a status this chain rejects.
    """
    issued: list[str] = []

    def _fake_stripe(url, *a, **kw):
        data = kw.get("data") or {}
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if "/payment_intents" in url and url.rstrip("/").endswith("payment_intents"):
            pi = f"pi_bounty_{len(issued)}_{uuid.uuid4().hex[:6]}"
            issued.append(pi)
            _PI_STORE[pi] = {
                "id": pi,
                "status": "requires_capture",       # held, not charged
                "amount": int(data.get("amount", 0)),
                "currency": data.get("currency", "usd"),
                "capture_method": data.get("capture_method", "automatic"),
                "metadata": {
                    k[len("metadata["):-1]: v for k, v in data.items()
                    if k.startswith("metadata[")
                },
            }
            resp.json.return_value = _PI_STORE[pi]
        elif "/capture" in url:
            pi = url.rsplit("/payment_intents/", 1)[-1].split("/")[0]
            _CAPTURED.append(pi)
            resp.json.return_value = {**_PI_STORE.get(pi, {"id": pi}), "status": "succeeded"}
        elif url.endswith("/transfers"):
            _TRANSFERS.append(data)
            resp.json.return_value = {"id": "tr_bounty", "status": "succeeded"}
        else:
            resp.json.return_value = {"id": "obj_unknown"}
        return resp

    def _fake_get(url, *a, **kw):
        pi = url.rsplit("/payment_intents/", 1)[-1].split("?")[0]
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = _PI_STORE.get(pi, {"id": pi, "status": "requires_payment_method"})
        return resp

    _PI_STORE.clear(); _CAPTURED.clear(); _TRANSFERS.clear()
    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_bounty_fixture"}), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post, \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock) as get:
        post.side_effect = _fake_stripe
        get.side_effect = _fake_get
        holder = MagicMock()
        holder.issued = issued
        holder.post = post
        yield holder


# Module-level so the fixture and the tests share one view of the fake Stripe.
_PI_STORE: dict[str, dict] = {}
_CAPTURED: list[str] = []
_TRANSFERS: list[dict] = []

# Every funding call now carries a payment credential, exactly as a paid room
# join does. Without it the relay answers 402 with a challenge — which is a
# behaviour worth having its own test rather than a fixture default.
_PAY = {"Authorization": "Payment spt_bounty_test_token"}


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
    return client.post("/bounties", json=body, headers={**_hdr(funder), **_PAY})


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


def test_no_hold_means_no_bounty(client, _escrow):
    """P72-5/P77: the prize must be *held* before the bounty row exists.

    Both failure shapes are covered because they arrive differently: an
    unconfigured rail raises RuntimeError, an unreachable Stripe surfaces as
    httpx.ConnectError. Catching only the first turned the second into a 500
    with a traceback instead of a refusal.
    """
    import httpx

    funder = _mk_agent(client, nous_id="nous_f_" + uuid.uuid4().hex[:6])
    before = client.get("/bounties").json()["count"]

    for exc in (RuntimeError("no Stripe key configured"),
                httpx.ConnectError("All connection attempts failed")):
        _escrow.post.side_effect = exc
        r = _post_bounty(client, funder)
        assert r.status_code == 402, \
            f"{type(exc).__name__} must refuse cleanly, got {r.status_code}: {r.text}"

    assert client.get("/bounties").json()["count"] == before, \
        "a bounty row must not exist when no hold does"


def test_funding_without_a_credential_is_a_402_not_a_bounty(client, _escrow):
    """P77: the funder pays the way every other payer does, so no credential
    gets the same answer every other unpaid request gets — a challenge.

    Previously `create_bounty` minted an unconfirmed PaymentIntent on its own
    and called that escrow, so this case did not exist: a bounty was created
    for a funder who had presented nothing at all.
    """
    funder = _mk_agent(client, nous_id="nous_f_" + uuid.uuid4().hex[:6])
    before = client.get("/bounties").json()["count"]
    r = client.post("/bounties", json={
        "title": "Unfunded", "description": "", "module_type": "emporia:chess:v1",
        "prize_cents": 5000,
        "entry_deadline": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }, headers=_hdr(funder))          # no Authorization: Payment header
    assert r.status_code == 402
    assert "spt_token" in r.json().get("message", ""), r.text
    assert client.get("/bounties").json()["count"] == before


def test_the_prize_is_authorised_not_charged(client, _escrow):
    """The distinction the whole lane rests on, asserted against the
    PaymentIntent the relay actually produced.

    P72-5 shipped a PI with no payment method and no confirm — it sat in
    `requires_payment_method`, holding nothing, and a mock returning an id
    could not tell that from a real hold. `requires_capture` is the status that
    means authorised-and-not-yet-charged; `manual` is what makes releasing it
    later possible.
    """
    funder = _mk_agent(client, nous_id="nous_f_" + uuid.uuid4().hex[:6])
    r = _post_bounty(client, funder, prize_cents=2500)
    assert r.status_code == 200 and r.json()["prize_escrowed"] is True

    pi = _PI_STORE[_escrow.issued[-1]]
    assert pi["status"] == "requires_capture", (
        f"the prize must be authorised and not yet charged, got {pi['status']!r} — "
        f"'requires_payment_method' means nothing is held"
    )
    assert pi["capture_method"] == "manual", "a charge cannot be released later"
    assert pi["amount"] == 2500
    assert pi["metadata"]["buyer_agent_id"] == funder
    assert pi["metadata"]["resource_type"] == "bounty"


def test_the_funder_may_withdraw_only_before_anyone_enters(client, _escrow):
    """The escrow protects the entrant's time, not just the funder's money."""
    from unittest.mock import AsyncMock, patch

    funder = _mk_agent(client, nous_id="nous_f_" + uuid.uuid4().hex[:6])
    entrant = _mk_agent(client, nous_id="nous_e_" + uuid.uuid4().hex[:6])
    listing_id = _post_bounty(client, funder).json()["listing_id"]

    with patch("emporia.routes.bounties.cancel_payment_hold",
               new_callable=AsyncMock) as cancel:
        cancel.return_value = {"status": "canceled"}
        # A stranger cannot withdraw someone else's prize.
        assert client.post(f"/bounties/{listing_id}/cancel",
                           headers=_hdr(entrant)).status_code == 403
        # The funder can, while nobody has committed.
        r = client.post(f"/bounties/{listing_id}/cancel", headers=_hdr(funder))
        assert r.status_code == 200 and r.json()["hold_released"] is True
        assert cancel.call_args.args[0] == _escrow.issued[-1], \
            "the hold released must be this bounty's own"

    # Once an agent has entered, the prize is committed.
    second = _post_bounty(client, funder).json()["listing_id"]
    assert client.post(f"/bounties/{second}/enter", headers=_hdr(entrant)).status_code == 200
    with patch("emporia.routes.bounties.cancel_payment_hold", new_callable=AsyncMock):
        r = client.post(f"/bounties/{second}/cancel", headers=_hdr(funder))
    assert r.status_code == 409 and "committed" in r.json()["detail"]


def test_a_full_contest_runs_and_hands_the_prize_to_the_session(client, _escrow):
    """P72-6: the whole settlement design in one test.

    The contest is an ordinary session, and the funder's escrowed
    PaymentIntent is recorded as that session's stake row. So the existing
    terminal path in routes/sessions.py captures it and pays the winner with no
    bounty-aware code anywhere in settlement — which is only possible because
    P72-1 established that settle() does not require the winner to be a payer.
    """
    from emporia import relay_server as rs

    funder = _mk_agent(client, nous_id="nous_f_" + uuid.uuid4().hex[:6])
    p1 = _mk_agent(client, nous_id="nous_p1_" + uuid.uuid4().hex[:6])
    p2 = _mk_agent(client, nous_id="nous_p2_" + uuid.uuid4().hex[:6])
    listing_id = _post_bounty(client, funder, prize_cents=4000).json()["listing_id"]

    for a in (p1, p2):
        assert client.post(f"/bounties/{listing_id}/enter", headers=_hdr(a)).status_code == 200

    r = client.post(f"/bounties/{listing_id}/start", headers=_hdr(p1))
    assert r.status_code == 200, r.text
    session_id = r.json()["session_id"]
    assert set(r.json()["participants"]) == {p1, p2}
    assert funder not in r.json()["participants"], "the funder does not compete"

    # The prize is now the session's stake, payable to whoever the module names.
    stakes = rs.get_session_payments(session_id)
    assert [s["payment_intent_id"] for s in stakes] == [_escrow.issued[-1]]
    assert sum(s["amount_cents"] for s in stakes) == 4000
    assert stakes[0]["agent_id"] == funder, "the funder is the only payer"

    # Entrants staked nothing.
    with rs.DB_LOCK, rs.get_db() as conn:
        paid = [dict(x) for x in conn.execute(
            "SELECT agent_id, stake_paid FROM participants WHERE session_id = ?",
            (session_id,)).fetchall()]
    assert all(p["stake_paid"] == 0 for p in paid), "entrants risk time, not money"


def test_a_bounty_cannot_seat_more_than_the_module_allows(client, _escrow):
    """The defect P72-4 shipped: chess seats two, and nothing stopped a third
    agent entering a contest they could never play in."""
    funder = _mk_agent(client, nous_id="nous_f_" + uuid.uuid4().hex[:6])
    listing_id = _post_bounty(client, funder).json()["listing_id"]
    for _ in range(2):
        a = _mk_agent(client, nous_id="nous_" + uuid.uuid4().hex[:8])
        assert client.post(f"/bounties/{listing_id}/enter", headers=_hdr(a)).status_code == 200
    third = _mk_agent(client, nous_id="nous_" + uuid.uuid4().hex[:8])
    r = client.post(f"/bounties/{listing_id}/enter", headers=_hdr(third))
    assert r.status_code == 403 and "full" in r.json()["detail"]


def test_a_contest_short_of_entrants_will_not_start(client, _escrow):
    funder = _mk_agent(client, nous_id="nous_f_" + uuid.uuid4().hex[:6])
    lone = _mk_agent(client, nous_id="nous_l_" + uuid.uuid4().hex[:6])
    listing_id = _post_bounty(client, funder).json()["listing_id"]
    client.post(f"/bounties/{listing_id}/enter", headers=_hdr(lone))
    r = client.post(f"/bounties/{listing_id}/start", headers=_hdr(lone))
    assert r.status_code == 409 and "Not ready" in r.json()["detail"]


def test_a_running_contest_cannot_be_started_twice(client, _escrow):
    """Two cron ticks racing must not seat the same entrants into two sessions,
    each holding a claim on one prize."""
    funder = _mk_agent(client, nous_id="nous_f_" + uuid.uuid4().hex[:6])
    p1 = _mk_agent(client, nous_id="nous_p1_" + uuid.uuid4().hex[:6])
    p2 = _mk_agent(client, nous_id="nous_p2_" + uuid.uuid4().hex[:6])
    listing_id = _post_bounty(client, funder).json()["listing_id"]
    for a in (p1, p2):
        client.post(f"/bounties/{listing_id}/enter", headers=_hdr(a))
    assert client.post(f"/bounties/{listing_id}/start", headers=_hdr(p1)).status_code == 200
    r = client.post(f"/bounties/{listing_id}/start", headers=_hdr(p2))
    assert r.status_code == 409 and "not open" in r.json()["detail"]


def test_capacity_is_enforced_at_the_modules_real_size(client, _escrow):
    """P72-8, pinned where the bug actually was.

    `validate_spec` read seats from the module registry; the route's
    `_spec_from_row` rebuilt a spec from the stored row and silently took
    BountySpec's (2, 2) defaults. A bounty on a four-seat module would be
    *validated* at four and then *enforced* at two — entry closing halfway
    through its own contest, with two agents refused from seats that existed.

    Invisible with the modules shipped today, which both seat two. So this
    registers a four-seat judging module for the duration and drives real
    entries through the API: the third and fourth must get in.

    tests/test_bounties.py asserts the same rule against `seats_for` directly
    and passes even with the route reverted — which is why this exists.
    """
    from emporia.module_sdk import MODULE_REGISTRY

    class _FourSeater:
        MODULE_TYPE = "emporia:fourseat:v1"
        DECLARES_WINNER = True
        MIN_PARTICIPANTS = 3
        MAX_PARTICIPANTS = 4

    MODULE_REGISTRY[_FourSeater.MODULE_TYPE] = _FourSeater
    try:
        funder = _mk_agent(client, nous_id="nous_f_" + uuid.uuid4().hex[:6])
        listing_id = _post_bounty(
            client, funder, module_type=_FourSeater.MODULE_TYPE
        ).json()["listing_id"]

        board = next(b for b in client.get("/bounties").json()["bounties"]
                     if b["listing_id"] == listing_id)
        assert (board["min_entrants"], board["max_entrants"]) == (3, 4), \
            f"the wire must carry the module's real seats, got {board}"

        for seat in range(4):
            a = _mk_agent(client, nous_id="nous_" + uuid.uuid4().hex[:8])
            r = client.post(f"/bounties/{listing_id}/enter", headers=_hdr(a))
            assert r.status_code == 200, (
                f"seat {seat + 1} of 4 was refused — capacity is being enforced at "
                f"the default rather than the module's size: {r.text}"
            )
        # Five is genuinely too many.
        fifth = _mk_agent(client, nous_id="nous_" + uuid.uuid4().hex[:8])
        r = client.post(f"/bounties/{listing_id}/enter", headers=_hdr(fifth))
        assert r.status_code == 403 and "full" in r.json()["detail"]
    finally:
        MODULE_REGISTRY.pop(_FourSeater.MODULE_TYPE, None)


def test_a_bounty_plays_to_the_end_and_the_winner_is_paid(client, _escrow):
    """P76: the claim the whole lane rests on, tested for the first time.

    Every pass so far proved a link: P72-1 that settle() pays a winner who
    never paid in, P72-6 that the funder's hold becomes the contest session's
    stake. Nothing had ever run the chain — post, enter, start, *play to a
    terminal result*, and check that money moved to the right agent.

    A claim assembled from proven links is not itself proven. This drives the
    whole thing: the module names a winner, the existing session settlement
    path captures the funder's hold, and the winner's payout is the prize less
    the operator fee. The funder is charged; the loser is not.

    `service` rather than `chess` because its terminal state is three
    deterministic actions instead of a checkmate — the money path is what is
    under test, not the referee.
    """
    from unittest.mock import AsyncMock, patch

    from emporia import relay_server as rs

    funder = _mk_agent(client, nous_id="nous_f_" + uuid.uuid4().hex[:6])
    # Real keypairs: /sessions/{id}/action requires a signature.
    buyer = "bty_buyer_" + uuid.uuid4().hex[:6]
    seller = "bty_seller_" + uuid.uuid4().hex[:6]
    for a in (buyer, seller):
        _register_keyed(client, a)
        with rs.DB_LOCK, rs.get_db() as conn:
            conn.execute(
                "UPDATE authorized_agents SET trust_level=?, nous_user_id=? WHERE agent_id=?",
                ("nous_verified", "nous_" + uuid.uuid4().hex[:8], a),
            )

    listing_id = _post_bounty(
        client, funder, prize_cents=4000, module_type="emporia:service:v1",
    ).json()["listing_id"]
    prize_pi = _escrow.issued[-1]

    # service seats participants[0] as buyer, [1] as seller — entry order decides.
    for a in (buyer, seller):
        assert client.post(f"/bounties/{listing_id}/enter",
                           headers=_hdr(a)).status_code == 200

    session_id = client.post(f"/bounties/{listing_id}/start",
                             headers=_hdr(buyer)).json()["session_id"]

    captured: list[str] = []
    transfers: list[dict] = []

    def _stripe(url, *a, **kw):
        if "/capture" in url:
            captured.append(url.rsplit("/payment_intents/", 1)[-1].split("/")[0])
        elif url.endswith("/transfers"):
            transfers.append(kw.get("data", {}))
        resp = MagicMock()
        resp.json.return_value = {"id": "tr_bounty_e2e", "status": "succeeded"}
        resp.raise_for_status = MagicMock()
        return resp

    with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_mock"}), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post, \
         patch.object(rs.AGENT_REGISTRY, "get_stripe_account", return_value="acct_winner"), \
         patch.object(rs.AGENT_REGISTRY, "get_stripe_account_status", return_value="active"), \
         patch.object(rs.AGENT_REGISTRY, "get_stripe_account_mode", return_value="test"):
        post.side_effect = _stripe
        for actor, kind, payload in (
            (seller, "accept", {}),
            (seller, "deliver", {"deliverable": "the work"}),
            (buyer, "confirm", {}),
        ):
            r = client.post(
                f"/sessions/{session_id}/action",
                json=_signed_action_body(client, session_id, actor, kind, payload,
                                    f"Bounty contest step: {kind} — proceeding per the service module contract."),
                headers=_hdr(actor),
            )
            assert r.status_code == 200, f"{kind} by {actor} failed: {r.text}"

    # The module named the seller; the money followed.
    assert captured == [prize_pi], \
        f"the funder's hold is what should have been captured, got {captured}"
    assert transfers and transfers[0].get("destination") == "acct_winner"
    assert int(transfers[0]["amount"]) == 3900, \
        "the winner receives the prize less the 2.5% operator fee"

    settlements = [dict(r) for r in rs.get_db().execute(
        "SELECT * FROM settlements WHERE session_id = ?", (session_id,)).fetchall()]
    assert len(settlements) == 1
    assert settlements[0]["winner_id"] == seller
    assert settlements[0]["status"] == "settled"
    assert settlements[0]["winner_payout_cents"] == 3900

    # And standing reflects it — one win for the seller, an entry for each.
    board = client.get("/leaderboard?limit=200").json()["standings"]
    rows = {r["agent_id"]: r for r in board}
    assert rows[seller]["wins"] == 1, "the winner should be on the table"
    assert rows[buyer]["wins"] == 0 and rows[buyer]["entries"] >= 1, \
        "the loser stays on the table with an entry and no win"



# ---------------------------------------------------------------------------
# P79: promised prizes. Nothing is committed at funding, so the consequence of
# not paying IS the product. These tests are the mechanism, not a check on it.
# ---------------------------------------------------------------------------

def _promise_bounty(client, funder, **kw):
    body = {
        "title": kw.get("title", "Promised contest"), "description": "",
        "module_type": "emporia:chess:v1", "rail": "x402",
        "prize_cents": kw.get("prize_cents", 5000),
        "entry_deadline": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    return client.post("/bounties", json=body, headers=_hdr(funder))


def _force_state(listing_id, **fields):
    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT metadata_json FROM listings WHERE listing_id = ?",
                           (listing_id,)).fetchone()
        meta = json.loads(row["metadata_json"]) | fields
        conn.execute("UPDATE listings SET metadata_json = ? WHERE listing_id = ?",
                     (json.dumps(meta), listing_id))
    return meta


def test_a_promised_bounty_needs_no_credential_and_says_it_is_not_escrowed(client, _escrow):
    """Nothing is committed at funding because nothing can be — so no credential
    is asked for, and the response must not claim a prize is escrowed."""
    funder = _mk_agent(client, nous_id="nous_p_" + uuid.uuid4().hex[:6])
    r = _promise_bounty(client, funder)
    assert r.status_code == 200, r.text
    assert r.json()["escrow_strength"] == "promised"
    assert r.json()["prize_escrowed"] is False, (
        "reporting a promise as escrow is the one collapse this whole model "
        "exists to prevent"
    )
    board = next(b for b in client.get("/bounties").json()["bounties"]
                 if b["listing_id"] == r.json()["listing_id"])
    assert board["escrow_strength"] == "promised"


def test_a_funder_who_owes_a_winner_cannot_promise_again(client, _escrow):
    """THE test. A promised prize is worth exactly the consequence of not
    paying it, and this is that consequence. If a defaulted funder can keep
    posting, the model is decorative and an entrant competing on credit has
    been misled."""
    funder = _mk_agent(client, nous_id="nous_d_" + uuid.uuid4().hex[:6])
    first = _promise_bounty(client, funder).json()["listing_id"]
    _force_state(first, state="defaulted", winner_id="somebody", prize_cents=5000)

    r = _promise_bounty(client, funder, title="Second attempt")
    assert r.status_code == 403 and "unpaid bounty default" in r.json()["detail"]

    # A held prize is still fine — the funder's word is in question, not their
    # money, and this rail does not ask anyone to take their word for anything.
    held = _post_bounty(client, funder)
    assert held.status_code == 200, held.text


def test_outstanding_promises_are_capped_through_the_api(client, _escrow):
    funder = _mk_agent(client, nous_id="nous_c_" + uuid.uuid4().hex[:6])
    for i in range(3):
        assert _promise_bounty(client, funder, title=f"Open {i}").status_code == 200
    r = _promise_bounty(client, funder, title="One too many")
    assert r.status_code == 403 and "limit is 3" in r.json()["detail"]


def test_the_default_flag_arrives_without_anyone_complaining(client, _escrow):
    """A consequence that waits for the winner to notice, know to ask, and have
    somewhere to ask is not a consequence."""
    from emporia.routes.bounties import reconcile_bounties

    funder = _mk_agent(client, nous_id="nous_r_" + uuid.uuid4().hex[:6])
    listing_id = _promise_bounty(client, funder).json()["listing_id"]
    _force_state(
        listing_id, state="awaiting_payment", winner_id="the_winner",
        payment_due_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )

    import anyio
    out = anyio.run(reconcile_bounties)
    assert listing_id in out["defaulted"]

    status = client.get(f"/bounties/{listing_id}/promise-status")
    assert status.status_code == 200 and status.json()["state"] == "defaulted"


def test_a_bounty_still_within_its_window_is_not_defaulted(client, _escrow):
    """Late is not the same as defaulted, and the window is what separates
    them. Flagging early would punish an agent for being asleep."""
    import anyio

    from emporia.routes.bounties import reconcile_bounties

    funder = _mk_agent(client, nous_id="nous_w_" + uuid.uuid4().hex[:6])
    listing_id = _promise_bounty(client, funder).json()["listing_id"]
    _force_state(
        listing_id, state="awaiting_payment", winner_id="the_winner",
        payment_due_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    out = anyio.run(reconcile_bounties)
    assert listing_id not in out["defaulted"]


def test_a_paid_held_bounty_stops_saying_it_is_running(client, _escrow):
    """P80: nothing moved a held bounty out of `running` — not the session
    settlement path, which pays the winner and knows nothing about bounties,
    and not settle_promise, which is x402-only. So a finished, paid contest sat
    on the board as still running, permanently.

    The money was always right. The board was wrong about it, which is the
    class of defect this dashboard has now been fixed for four times.
    """
    import anyio

    from emporia import relay_server as rs
    from emporia.routes.bounties import reconcile_bounties

    funder = _mk_agent(client, nous_id="nous_h_" + uuid.uuid4().hex[:6])
    listing_id = _post_bounty(client, funder).json()["listing_id"]
    _force_state(listing_id, state="running", session_id="ses_held_done")
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute(
            "INSERT INTO settlements (settlement_id, session_id, winner_id, status, "
            "created_at) VALUES (?,?,?,?,?)",
            (f"stl_{uuid.uuid4().hex[:8]}", "ses_held_done", "the_winner", "settled",
             datetime.now(UTC).isoformat()),
        )

    out = anyio.run(reconcile_bounties)
    assert listing_id in out["settled"]
    assert client.get(f"/bounties/{listing_id}/promise-status").json()["state"] == "settled"


def test_a_refunded_contest_does_not_strand_the_bounty(client, _escrow):
    """A dispute, timeout or undecidable draw ends the session with no winner.
    P79 only looked at `status == "settled"`, so those bounties stayed
    `running` forever — and on the promised rail that consumed one of the
    funder's three outstanding slots permanently, for a contest that produced
    nothing."""
    import anyio

    from emporia import relay_server as rs
    from emporia.routes.bounties import reconcile_bounties

    funder = _mk_agent(client, nous_id="nous_rf_" + uuid.uuid4().hex[:6])
    listing_id = _promise_bounty(client, funder).json()["listing_id"]
    _force_state(listing_id, state="running", session_id="ses_refunded")
    with rs.DB_LOCK, rs.get_db() as conn:
        conn.execute(
            "INSERT INTO settlements (settlement_id, session_id, winner_id, status, "
            "created_at) VALUES (?,?,?,?,?)",
            (f"stl_{uuid.uuid4().hex[:8]}", "ses_refunded", "nominal_winner", "refunded",
             datetime.now(UTC).isoformat()),
        )

    out = anyio.run(reconcile_bounties)
    assert listing_id in out["refunded"]

    # And the slot is free again — the funder can promise once more.
    assert _promise_bounty(client, funder, title="After the refund").status_code == 200


def test_a_contest_still_being_played_is_left_alone(client, _escrow):
    """No settlement row yet means the module has not finished. Advancing on
    that would mark a live contest resolved."""
    import anyio

    from emporia.routes.bounties import reconcile_bounties

    funder = _mk_agent(client, nous_id="nous_live_" + uuid.uuid4().hex[:6])
    listing_id = _promise_bounty(client, funder).json()["listing_id"]
    _force_state(listing_id, state="running", session_id="ses_still_playing")
    out = anyio.run(reconcile_bounties)
    assert listing_id not in sum(out.values(), [])


def test_the_reconciler_is_actually_wired_to_a_loop():
    """P80's real defect: P79 wrote the reconciler, tested it by calling it
    directly, and connected it to nothing.

    That is not a missing convenience. The automatic default flag is the only
    thing backing a promised prize, so a reconciler nobody runs means a funder
    can take an agent's work and never pay, with nothing anywhere noticing —
    and every test still passes, because the tests call it themselves.

    Exactly the shape of P72-4, where bounties.py was a well-tested module with
    no callers. Asserted against the lifespan source rather than by starting a
    relay, so it stays true for the process that actually serves traffic.
    """
    import inspect

    from emporia import relay_server as rs

    lifespan_src = inspect.getsource(rs.lifespan)
    assert "_bounty_reconcile_loop" in lifespan_src, (
        "the bounty reconciler must be started by the relay, not only by tests"
    )
    loop_src = inspect.getsource(rs._bounty_reconcile_loop)
    assert "reconcile_bounties" in loop_src
    # N12-1's rule: one raise must not end the loop for the life of the process.
    assert "continue" in loop_src and "except Exception" in loop_src, (
        "a loop that dies on one error is indistinguishable from the defect it "
        "exists to fix"
    )
