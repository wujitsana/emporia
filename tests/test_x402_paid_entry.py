"""Paid room and Agora entry over x402: who gets in, in what order, and what a refusal leaves behind.

Ported from the removed card tests (the PaymentIntent versions in test_emporia.py): gate order, invite plus
payment, re-entry while already entitled, a kicked member who cannot come back, access that expires, a
replayed payment, and a refusal that leaves no membership. The facilitator is faked at
`payments.x402_verify_payment` / `x402_settle_payment`; everything above it is the relay's own code.
"""
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from emporia import rooms as rooms_db
from emporia.relay_server import DATABASE_PATH, get_db
from tests.test_emporia import (
    _X402_ENV, _X402_EXTRA, _register, _register_keyed, _signed_kick, _signed_room,
    _x402_payment_signature_header,
)

OWNER_WALLET = "0x" + "ab" * 20


@pytest.fixture
def pay():
    """x402 on, a facilitator that accepts every payment and settles each under a fresh transaction hash."""
    seen = []

    async def settle(payload, requirements):
        tx = "0xtx_" + uuid.uuid4().hex[:12]
        seen.append(tx)
        return {"success": True, "transaction": tx, "network": "eip155:8453"}

    verify = AsyncMock(return_value={"isValid": True, "payer": "0x" + "cc" * 20})
    with patch.dict(os.environ, _X402_ENV, clear=False), \
         patch("emporia.payments.x402_verify_payment", new=verify), \
         patch("emporia.payments.x402_settle_payment", new=AsyncMock(side_effect=settle)) as settled:
        yield type("Pay", (), {"verify": verify, "settle": settled, "txs": seen})


def _header(cents: int) -> str:
    return _x402_payment_signature_header({
        "scheme": "exact", "network": "eip155:8453", "amount": str(cents * 10_000),
        "asset": "0xUSDCFAKE", "payTo": OWNER_WALLET, "maxTimeoutSeconds": 300, "extra": _X402_EXTRA,
    })


def _ids(prefix: str):
    suffix = uuid.uuid4().hex[:8]
    return f"{prefix}_o_{suffix}", f"{prefix}_p_{suffix}"


def _owner_with_wallet(client, owner: str) -> dict:
    _register_keyed(client, owner)
    r = client.post(f"/agents/{owner}/x402-wallet",
                    json=__import__("conftest").signed_wallet_body(owner, "evm", OWNER_WALLET),
                    headers={"x-emporia-agent-id": owner})
    assert r.status_code == 200, r.text
    return {"x-emporia-agent-id": owner}


def _paid_room(client, owner: str, cents: int = 100, **extra) -> str:
    r = client.post("/rooms", json=_signed_room(owner, {
        "name": "Paid " + owner, "creator_id": owner, "room_type": "private", "gate_type": "paid",
        "entry_fee_cents": cents, **extra}), headers={"x-emporia-agent-id": owner})
    assert r.status_code == 200, r.text
    return r.json()["room_id"]


def _join(client, room_id: str, payer: str, cents: int | None = None):
    headers = {"x-emporia-agent-id": payer}
    if cents is not None:
        headers["PAYMENT-SIGNATURE"] = _header(cents)
    return client.post(f"/rooms/{room_id}/join", json={"agent_id": payer}, headers=headers)


def _count(sql: str, *args) -> int:
    with get_db() as conn:
        return conn.execute(sql, args).fetchone()[0]


def _expire(resource_type: str, resource_id: str, agent: str) -> None:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute("UPDATE access_entitlements SET expires_at = ? WHERE resource_type=? AND resource_id=? AND agent_id=?",
                 ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), resource_type, resource_id, agent))
    conn.commit()
    conn.close()


# -- gate order and refusals --------------------------------------------------

def test_a_full_paid_room_refuses_before_any_payment_is_verified(client, pay):
    owner, payer = _ids("xfull")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    room_id = _paid_room(client, owner, max_members=1)    # the creator only: already full
    r = _join(client, room_id, payer, 100)
    assert r.status_code == 403, r.text
    assert not rooms_db.is_member(room_id, payer, db_path=DATABASE_PATH)
    assert _count("SELECT COUNT(*) FROM payments WHERE room_id = ?", room_id) == 0
    assert not pay.verify.called and not pay.settle.called, "capacity is known before any payment work"


def test_a_refused_payment_leaves_no_membership_no_entitlement_no_payment(client, pay):
    owner, payer = _ids("xrefuse")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    room_id = _paid_room(client, owner)
    pay.verify.return_value = {"isValid": False, "invalidReason": "insufficient_funds"}
    r = _join(client, room_id, payer, 100)
    assert r.status_code == 402, r.text
    assert not rooms_db.is_member(room_id, payer, db_path=DATABASE_PATH)
    assert _count("SELECT COUNT(*) FROM payments WHERE room_id = ?", room_id) == 0
    assert _count("SELECT COUNT(*) FROM access_entitlements WHERE resource_id = ? AND agent_id = ?", room_id, payer) == 0
    assert _count("SELECT COUNT(*) FROM settlements WHERE session_id = ?", room_id) == 0
    assert not pay.settle.called, "a payment the facilitator would not verify is never settled"


def test_an_unpaid_join_is_a_402_that_offers_x402_and_nothing_else(client, pay):
    owner, payer = _ids("xoffer")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    room_id = _paid_room(client, owner)
    r = _join(client, room_id, payer)
    assert r.status_code == 402
    body = r.json()
    assert body["x402"]["accepts"][0]["payTo"] == OWNER_WALLET
    assert body["protocol"] == "emporia:v1"
    assert "card" not in body
    assert "WWW-Authenticate" not in r.headers and "X-MPP-Version" not in r.headers
    assert not rooms_db.is_member(room_id, payer, db_path=DATABASE_PATH)


# -- re-entry, expiry, kicks, replays -----------------------------------------

def test_re_entry_while_entitled_is_free_and_settles_once(client, pay):
    owner, payer = _ids("xreenter")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    room_id = _paid_room(client, owner)
    assert _join(client, room_id, payer, 100).status_code == 200
    rooms_db.remove_member(room_id, payer, db_path=DATABASE_PATH)        # it left: its entitlement stands
    again = _join(client, room_id, payer)                                 # no payment at all
    assert again.status_code == 200, again.text
    assert rooms_db.is_member(room_id, payer, db_path=DATABASE_PATH)
    assert _count("SELECT COUNT(*) FROM payments WHERE room_id = ?", room_id) == 1
    assert _count("SELECT COUNT(*) FROM settlements WHERE session_id = ?", room_id) == 1
    assert len(pay.txs) == 1


def test_a_repurchase_after_the_access_window_is_a_second_payment_with_its_own_settlement(client, pay):
    owner, payer = _ids("xexpire")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    room_id = _paid_room(client, owner, access_duration_days=1)
    assert _join(client, room_id, payer, 100).status_code == 200
    _expire("room", room_id, payer)
    rooms_db.remove_member(room_id, payer, db_path=DATABASE_PATH)
    assert _join(client, room_id, payer).status_code == 402, "an expired window is not free re-entry"
    assert _join(client, room_id, payer, 100).status_code == 200
    assert _count("SELECT COUNT(*) FROM payments WHERE room_id = ?", room_id) == 2
    assert _count("SELECT COUNT(*) FROM settlements WHERE session_id = ?", room_id) == 2
    assert len(set(pay.txs)) == 2


def test_a_kicked_payer_cannot_rejoin_with_a_fresh_payment(client, pay):
    owner, payer = _ids("xkick")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    room_id = _paid_room(client, owner)
    assert _join(client, room_id, payer, 100).status_code == 200
    kicked = client.post(f"/rooms/{room_id}/kick",
                         json=_signed_kick(owner, room_id, {"target_agent_id": payer}),
                         headers={"x-emporia-agent-id": owner})
    assert kicked.status_code == 200, kicked.text
    settled_before = len(pay.txs)
    assert _join(client, room_id, payer, 100).status_code == 403
    assert not rooms_db.is_member(room_id, payer, db_path=DATABASE_PATH)
    assert len(pay.txs) == settled_before, "a refused rejoin must not take a second payment"
    assert _count("SELECT COUNT(*) FROM settlements WHERE session_id = ?", room_id) == 1


def test_a_kick_never_moves_money_and_a_refund_request_is_refused(client, pay):
    owner, payer = _ids("xnorefund")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    room_id = _paid_room(client, owner)
    assert _join(client, room_id, payer, 100).status_code == 200
    asked = client.post(f"/rooms/{room_id}/kick",
                        json=_signed_kick(owner, room_id, {"target_agent_id": payer, "refund": True}),
                        headers={"x-emporia-agent-id": owner})
    assert asked.status_code == 400 and "does not refund" in asked.text
    assert rooms_db.is_member(room_id, payer, db_path=DATABASE_PATH), "the refused kick changed nothing"
    plain = client.post(f"/rooms/{room_id}/kick",
                        json=_signed_kick(owner, room_id, {"target_agent_id": payer}),
                        headers={"x-emporia-agent-id": owner})
    assert plain.status_code == 200 and "refund" not in plain.json()
    assert _count("SELECT COUNT(*) FROM payments WHERE room_id = ? AND status = 'confirmed'", room_id) == 1


def test_a_replayed_payment_is_refused_for_another_agent(client, pay):
    owner, payer = _ids("xreplay")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    thief = "xthief_" + uuid.uuid4().hex[:8]
    _register(client, thief)
    room_id = _paid_room(client, owner)
    assert _join(client, room_id, payer, 100).status_code == 200
    [tx] = pay.txs
    # The facilitator hands the same settled transaction back for the second agent's credential.
    pay.settle.side_effect = AsyncMock(return_value={"success": True, "transaction": tx, "network": "eip155:8453"})
    r = _join(client, room_id, thief, 100)
    assert r.status_code == 402, r.text
    assert not rooms_db.is_member(room_id, thief, db_path=DATABASE_PATH)
    assert _count("SELECT COUNT(*) FROM payments WHERE payment_intent_id = ?", tx) == 1


# -- a paid Agora topic: the invite and the payment --------------------------

def _paid_topic(client, owner: str, cents: int = 100):
    r = client.post("/agoras/topics", json={
        "name": "Paid topic " + owner, "description": "d", "visibility": "private",
        "gate_type": "paid_invite", "entry_fee_cents": cents, "creator_id": owner},
        headers={"x-emporia-agent-id": owner})
    assert r.status_code == 200, r.text
    return r.json()["slug"], r.json()["topic_id"]


def _subscribe(client, slug: str, agent: str, cents: int | None = None):
    headers = {"x-emporia-agent-id": agent}
    if cents is not None:
        headers["PAYMENT-SIGNATURE"] = _header(cents)
    return client.post(f"/agoras/topics/{slug}/subscribe", json={"agent_id": agent}, headers=headers)


def _member(topic_id: str, agent: str) -> bool:
    return _count("SELECT COUNT(*) FROM agora_members WHERE topic_id = ? AND agent_id = ?", topic_id, agent) > 0


def test_a_paid_topic_needs_the_invite_and_the_payment_and_checks_the_invite_first(client, pay):
    owner, invited = _ids("xagora")
    stranger = "xstranger_" + uuid.uuid4().hex[:8]
    _owner_with_wallet(client, owner)
    _register(client, invited)
    _register(client, stranger)
    slug, topic_id = _paid_topic(client, owner)
    client.post(f"/agoras/topics/{slug}/invite", json={"invitee_id": invited},
                headers={"x-emporia-agent-id": owner})

    refused = _subscribe(client, slug, stranger, 100)               # paying does not replace the invite
    assert refused.status_code == 403, refused.text
    assert not pay.verify.called, "the invite is checked before any payment work"

    unpaid = _subscribe(client, slug, invited)                      # an invite alone is not entry
    assert unpaid.status_code == 402 and unpaid.json()["x402"]["accepts"]
    assert not _member(topic_id, invited)

    ok = _subscribe(client, slug, invited, 100)                     # invite and payment together
    assert ok.status_code == 200, ok.text
    assert _member(topic_id, invited) and not _member(topic_id, stranger)
    assert _count("SELECT COUNT(*) FROM settlements WHERE session_id = ?", topic_id) == 1


def test_an_unsubscribed_member_resubscribes_free_within_the_window_and_pays_again_after_it(client, pay):
    owner, payer = _ids("xresub")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    slug, topic_id = _paid_topic(client, owner)
    client.post(f"/agoras/topics/{slug}/invite", json={"invitee_id": payer},
                headers={"x-emporia-agent-id": owner})
    assert _subscribe(client, slug, payer, 100).status_code == 200
    gone = client.delete(f"/agoras/topics/{slug}/subscribe", params={"agent_id": payer},
                         headers={"x-emporia-agent-id": payer})
    assert gone.status_code == 200, gone.text
    assert _subscribe(client, slug, payer).status_code == 200       # no invite, no payment: the window is open
    assert _count("SELECT COUNT(*) FROM settlements WHERE session_id = ?", topic_id) == 1
    assert len(pay.txs) == 1

    client.delete(f"/agoras/topics/{slug}/subscribe", params={"agent_id": payer},
                  headers={"x-emporia-agent-id": payer})
    _expire("agora", topic_id, payer)
    assert _subscribe(client, slug, payer).status_code == 402       # the window closed: pay again
    assert _subscribe(client, slug, payer, 100).status_code == 200
    assert _count("SELECT COUNT(*) FROM settlements WHERE session_id = ?", topic_id) == 2


def test_a_replayed_agora_payment_is_refused_before_membership(client, pay):
    owner, legit = _ids("xagreplay")
    attacker = "xatk_" + uuid.uuid4().hex[:8]
    _owner_with_wallet(client, owner)
    _register(client, legit)
    _register(client, attacker)
    slug, topic_id = _paid_topic(client, owner)
    for who in (legit, attacker):
        client.post(f"/agoras/topics/{slug}/invite", json={"invitee_id": who}, headers={"x-emporia-agent-id": owner})
    assert _subscribe(client, slug, legit, 100).status_code == 200
    [tx] = pay.txs
    pay.settle.side_effect = AsyncMock(return_value={"success": True, "transaction": tx, "network": "eip155:8453"})
    r = _subscribe(client, slug, attacker, 100)
    assert r.status_code == 402, r.text
    assert not _member(topic_id, attacker), "a rejected replay must not leave the attacker a member"


# -- what a paid entry leaves behind: reads, and the member's own receipt -----

def test_an_expired_room_entitlement_blocks_the_private_room_read(client, pay):
    owner, payer = _ids("xroomread")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    room_id = _paid_room(client, owner, access_duration_days=1)
    assert _join(client, room_id, payer, 100).status_code == 200
    hdr = {"x-emporia-agent-id": payer}
    assert client.get(f"/rooms/{room_id}", headers=hdr).status_code == 200
    _expire("room", room_id, payer)
    assert client.get(f"/rooms/{room_id}", headers=hdr).status_code == 403


def test_an_expired_topic_entitlement_blocks_the_private_topic_read(client, pay):
    owner, payer = _ids("xtopicread")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    r = client.post("/agoras/topics", json={
        "name": "Expiring " + owner, "description": "d", "visibility": "private", "gate_type": "paid_invite",
        "entry_fee_cents": 100, "creator_id": owner, "access_duration_days": 1},
        headers={"x-emporia-agent-id": owner})
    slug, topic_id = r.json()["slug"], r.json()["topic_id"]
    client.post(f"/agoras/topics/{slug}/invite", json={"invitee_id": payer}, headers={"x-emporia-agent-id": owner})
    assert _subscribe(client, slug, payer, 100).status_code == 200
    hdr = {"x-emporia-agent-id": payer}
    assert client.get(f"/agoras/topics/{slug}", headers=hdr).status_code == 200
    _expire("agora", topic_id, payer)
    assert client.get(f"/agoras/topics/{slug}", headers=hdr).status_code == 403


def test_a_kicked_member_still_reads_its_own_receipt_and_a_stranger_cannot(client, pay):
    owner, payer = _ids("xreceipt")
    stranger = "xrs_" + uuid.uuid4().hex[:8]
    _owner_with_wallet(client, owner)
    _register(client, payer)
    _register(client, stranger)
    room_id = _paid_room(client, owner)
    assert _join(client, room_id, payer, 100).status_code == 200
    kicked = client.post(f"/rooms/{room_id}/kick", json=_signed_kick(owner, room_id, {"target_agent_id": payer}),
                         headers={"x-emporia-agent-id": owner})
    assert kicked.status_code == 200, kicked.text
    own = client.get("/payments/settlements", params={"agent_id": payer}, headers={"x-emporia-agent-id": payer})
    assert own.status_code == 200, own.text
    assert any(s["session_id"] == room_id for s in own.json()["settlements"])
    assert client.get("/payments/settlements", params={"agent_id": payer},
                      headers={"x-emporia-agent-id": stranger}).status_code == 403


# -- more of what the removed card tests guarded, on x402 ---------------------

def test_an_expired_member_drops_out_of_the_room_listing_while_a_current_one_keeps_the_roster(client, pay):
    owner, payer = _ids("xlist")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    room_id = _paid_room(client, owner, access_duration_days=1)
    assert _join(client, room_id, payer, 100).status_code == 200
    hdr = {"x-emporia-agent-id": payer}
    mine = next(r for r in client.get("/rooms", headers=hdr).json()["rooms"] if r["room_id"] == room_id)
    assert set(mine["members"]) == {owner, payer}
    owner_view = next(r for r in client.get("/rooms", headers={"x-emporia-agent-id": owner}).json()["rooms"]
                      if r["room_id"] == room_id)
    assert set(owner_view["members"]) == {owner, payer}
    _expire("room", room_id, payer)
    assert not any(r["room_id"] == room_id for r in client.get("/rooms", headers=hdr).json()["rooms"])
    owner_after = next(r for r in client.get("/rooms", headers={"x-emporia-agent-id": owner}).json()["rooms"]
                       if r["room_id"] == room_id)
    assert owner_after["room_id"] == room_id, "the creator is unaffected by a member's expiry"


def test_an_expired_member_loses_the_resource_view_but_keeps_its_own_receipt(client, pay):
    owner, payer = _ids("xview")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    room_id = _paid_room(client, owner, access_duration_days=1)
    assert _join(client, room_id, payer, 100).status_code == 200
    _expire("room", room_id, payer)
    hdr = {"x-emporia-agent-id": payer}
    assert client.get(f"/payments/settlements/{room_id}", headers=hdr).status_code == 403
    own = client.get("/payments/settlements", params={"agent_id": payer}, headers=hdr)
    assert any(s["session_id"] == room_id for s in own.json()["settlements"])


def test_an_unsubscribed_topic_member_still_reads_its_own_receipt(client, pay):
    owner, payer = _ids("xunsub")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    slug, topic_id = _paid_topic(client, owner)
    client.post(f"/agoras/topics/{slug}/invite", json={"invitee_id": payer}, headers={"x-emporia-agent-id": owner})
    assert _subscribe(client, slug, payer, 100).status_code == 200
    client.delete(f"/agoras/topics/{slug}/subscribe", params={"agent_id": payer}, headers={"x-emporia-agent-id": payer})
    own = client.get("/payments/settlements", params={"agent_id": payer}, headers={"x-emporia-agent-id": payer})
    assert own.status_code == 200 and any(s["session_id"] == topic_id for s in own.json()["settlements"])


def test_a_public_paid_topic_gates_reads_and_posts_until_the_entry_fee_is_paid(client, pay):
    from tests.topic_posts import v2_post
    owner, payer = _ids("xpub")
    stranger = "xps_" + uuid.uuid4().hex[:8]
    _owner_with_wallet(client, owner)
    _register_keyed(client, payer)
    _register_keyed(client, stranger)
    r = client.post("/agoras/topics", json={
        "name": "Public paid " + owner, "description": "d", "visibility": "public", "gate_type": "paid_invite",
        "entry_fee_cents": 100, "creator_id": owner}, headers={"x-emporia-agent-id": owner})
    slug = r.json()["slug"]
    sh = {"x-emporia-agent-id": stranger}
    assert client.get(f"/agoras/topics/{slug}/posts", headers=sh).status_code == 403
    assert v2_post(client, stranger, slug, "never paid a cent").status_code == 403
    assert any(t["slug"] == slug for t in client.get("/agoras/topics", headers=sh).json()["topics"])
    assert client.get(f"/agoras/topics/{slug}/posts", headers={"x-emporia-agent-id": owner}).status_code == 200
    client.post(f"/agoras/topics/{slug}/invite", json={"invitee_id": payer}, headers={"x-emporia-agent-id": owner})
    assert _subscribe(client, slug, payer, 100).status_code == 200
    assert client.get(f"/agoras/topics/{slug}/posts", headers={"x-emporia-agent-id": payer}).status_code == 200
    assert v2_post(client, payer, slug, "paid up").status_code == 200


def test_a_co_payer_sees_the_others_payment_rows_redacted_not_omitted(client, pay):
    owner, a = _ids("xco")
    b = "xcob_" + uuid.uuid4().hex[:8]
    _owner_with_wallet(client, owner)
    _register(client, a)
    _register(client, b)
    room_id = _paid_room(client, owner)
    assert _join(client, room_id, a, 100).status_code == 200
    assert _join(client, room_id, b, 100).status_code == 200
    tx_a, tx_b = pay.txs
    ha = {"x-emporia-agent-id": a}
    own = client.get("/payments/settlements", params={"agent_id": a}, headers=ha).json()["settlements"]
    seen = {i for s in own for i in s["payment_intent_ids"]}
    assert tx_a in seen and tx_b not in seen
    by_room = client.get("/payments/settlements", params={"session_id": room_id}, headers=ha).json()["settlements"]
    assert tx_b not in {i for s in by_room for i in s["payment_intent_ids"]}
    assert any(s["payment_intent_ids"] == ["[redacted]"] for s in by_room)
    rec = client.get("/payments/records", params={"room_id": room_id}, headers=ha)
    assert rec.status_code == 200
    rows = rec.json()["payments"]
    assert any(p["payment_intent_id"] == tx_a for p in rows)
    redacted = [p for p in rows if p["payment_intent_id"] == "[redacted]"]
    assert redacted and all(p["agent_id"] == "[redacted]" for p in redacted)


def test_a_topic_co_payer_cannot_read_the_others_payment_references(client, pay):
    owner, a = _ids("xcot")
    b = "xcotb_" + uuid.uuid4().hex[:8]
    _owner_with_wallet(client, owner)
    _register(client, a)
    _register(client, b)
    slug, topic_id = _paid_topic(client, owner)
    for who in (a, b):
        client.post(f"/agoras/topics/{slug}/invite", json={"invitee_id": who}, headers={"x-emporia-agent-id": owner})
        assert _subscribe(client, slug, who, 100).status_code == 200
    tx_a, tx_b = pay.txs
    rows = client.get("/payments/settlements", params={"session_id": topic_id},
                      headers={"x-emporia-agent-id": a}).json()["settlements"]
    seen = {i for s in rows for i in s["payment_intent_ids"]}
    assert tx_b not in seen
    # an anonymous caller reads nothing about a paid topic's settlements
    assert client.get(f"/payments/settlements/{topic_id}").status_code == 403


def test_a_public_paid_topic_post_list_opens_no_connection_per_row(client, pay):
    from emporia import entitlements
    from tests.topic_posts import v2_post
    owner, viewer = _ids("xconn")
    _owner_with_wallet(client, owner)
    _register_keyed(client, viewer)
    r = client.post("/agoras/topics", json={
        "name": "Conn " + owner, "description": "d", "visibility": "public", "gate_type": "paid_invite",
        "entry_fee_cents": 100, "creator_id": owner}, headers={"x-emporia-agent-id": owner})
    slug = r.json()["slug"]
    for i in range(5):
        assert v2_post(client, owner, slug, f"post {i}").status_code == 200
    client.post(f"/agoras/topics/{slug}/invite", json={"invitee_id": viewer}, headers={"x-emporia-agent-id": owner})
    assert _subscribe(client, slug, viewer, 100).status_code == 200
    n = {"n": 0}
    original = entitlements._connect

    def counting(db_path=None):
        n["n"] += 1
        return original(db_path)

    with patch.object(entitlements, "_connect", side_effect=counting):
        resp = client.get(f"/agents/{owner}/posts", headers={"x-emporia-agent-id": viewer})
    assert resp.status_code == 200 and len(resp.json()["posts"]) == 5
    assert n["n"] == 0, f"the entitlement is checked on the open request connection, got {n['n']} for 5 posts"


def test_an_agora_subscribe_that_crashes_at_the_grant_is_repaired_by_the_retry(client, pay):
    from emporia import entitlements
    owner, payer = _ids("xcrasht")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    slug, topic_id = _paid_topic(client, owner)
    client.post(f"/agoras/topics/{slug}/invite", json={"invitee_id": payer}, headers={"x-emporia-agent-id": owner})
    with patch.object(entitlements, "grant", side_effect=RuntimeError("relay died mid-grant")):
        with pytest.raises(RuntimeError):
            _subscribe(client, slug, payer, 100)
    assert not entitlements.has_access("agora", topic_id, payer, db_path=DATABASE_PATH)
    assert _subscribe(client, slug, payer, 100).status_code == 200
    assert entitlements.has_access("agora", topic_id, payer, db_path=DATABASE_PATH)
    assert _count("SELECT COUNT(*) FROM settlements WHERE session_id = ?", topic_id) == 1
    assert len(pay.txs) == 1, "the retry resumed the settled purchase; it was not bought a second time"


def test_a_crash_after_the_grant_is_finished_without_a_second_receipt(client, pay):
    from emporia import relay_server as rs
    owner, payer = _ids("xcrashr")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    room_id = _paid_room(client, owner)
    with patch.object(rs, "record_settlement", side_effect=RuntimeError("relay died after the grant")):
        with pytest.raises(RuntimeError):
            _join(client, room_id, payer, 100)
    assert _count("SELECT COUNT(*) FROM access_entitlements WHERE resource_id = ? AND agent_id = ?", room_id, payer) == 1
    assert _join(client, room_id, payer).status_code == 200
    assert _count("SELECT COUNT(*) FROM access_entitlements WHERE resource_id = ? AND agent_id = ?", room_id, payer) == 1, \
        "finishing the purchase issues no second receipt"
    assert _count("SELECT COUNT(*) FROM settlements WHERE session_id = ?", room_id) == 1
    assert len(pay.txs) == 1


def test_a_repurchase_after_expiry_holds_the_entitlement_it_paid_for_and_every_table_names_the_rail(client, pay):
    from emporia import entitlements
    owner, payer = _ids("xrail")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    room_id = _paid_room(client, owner, access_duration_days=30)
    assert _join(client, room_id, payer, 100).status_code == 200
    _expire("room", room_id, payer)
    rooms_db.remove_member(room_id, payer, db_path=DATABASE_PATH)
    assert not entitlements.has_access("room", room_id, payer, db_path=DATABASE_PATH)
    assert _join(client, room_id, payer, 100).status_code == 200
    assert entitlements.has_access("room", room_id, payer, db_path=DATABASE_PATH), "the repurchase must restore access"
    with get_db() as conn:
        for table, col in (("payments", "room_id"), ("settlements", "session_id"), ("access_entitlements", "resource_id")):
            rails = {r[0] for r in conn.execute(f"SELECT DISTINCT rail FROM {table} WHERE {col} = ?", (room_id,))}
            assert rails == {"x402"}, table


def test_a_kick_bans_only_that_agent_from_that_room(client, pay):
    owner, a = _ids("xscope")
    b = "xscb_" + uuid.uuid4().hex[:8]
    _owner_with_wallet(client, owner)
    _register(client, a)
    _register(client, b)
    room_id = _paid_room(client, owner)
    other = _paid_room(client, owner)
    assert _join(client, room_id, a, 100).status_code == 200
    client.post(f"/rooms/{room_id}/kick", json=_signed_kick(owner, room_id, {"target_agent_id": a}),
                headers={"x-emporia-agent-id": owner})
    assert _join(client, room_id, b, 100).status_code == 200, "another agent joins the same room"
    assert _join(client, other, a, 100).status_code == 200, "the kicked agent joins a different room"


def test_both_doors_advertise_the_same_rails_and_nothing_about_cards(client, pay):
    owner, _ = _ids("xdoors")
    _register(client, owner)
    health = client.get("/health").json()
    profile = client.get(f"/agents/{owner}", headers={"x-emporia-agent-id": owner}).json()
    assert health["payment_rails"] == profile["payment_rails"] == ["free", "x402"]
    assert not {"stripe_enabled", "mpp_enabled", "payment_methods", "cards_paused"} & set(health)


def test_an_expired_topic_entitlement_renews_while_still_subscribed_without_a_new_invite(client, pay):
    owner, payer = _ids("xrenew")
    _owner_with_wallet(client, owner)
    _register(client, payer)
    slug, topic_id = _paid_topic(client, owner)
    client.post(f"/agoras/topics/{slug}/invite", json={"invitee_id": payer}, headers={"x-emporia-agent-id": owner})
    assert _subscribe(client, slug, payer, 100).status_code == 200
    _expire("agora", topic_id, payer)
    assert _subscribe(client, slug, payer).status_code == 402, "an expired window asks for payment again"
    assert _subscribe(client, slug, payer, 100).status_code == 200, "and needs no new invite"
    assert _count("SELECT COUNT(*) FROM settlements WHERE session_id = ?", topic_id) == 2
    assert len(set(pay.txs)) == 2
