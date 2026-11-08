"""P275: a payment pays the agent whose own signature stands behind what is
bought — a room fee its creator, at most the fee it signed; a stake or a
purchase in a game another player seated by its own signed join."""
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from emporia import identity, mcp_server, payee, wire
from emporia.relay_server import app
from tests.test_agent_wallet import NET, _FakeHttpx, _Resp, wallet_env  # noqa: F401  (fixture)
from tests.test_emporia import _register_keyed, _signed_invite, _signed_room


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51275))


def _as(agent_id: str) -> dict:
    return {"x-emporia-agent-id": agent_id}


def test_a_room_fee_pays_its_creator_at_most_the_fee_it_signed(client):
    _register_keyed(client, "pb_owner")
    r = client.post("/rooms", json=_signed_room("pb_owner", {
        "name": "pb room", "room_type": "private", "gate_type": "paid", "entry_fee_cents": 150}),
        headers=_as("pb_owner"))
    room = client.get(f"/rooms/{r.json()['room_id']}", headers=_as("pb_owner")).json()
    key = identity.get_public_key_hex("pb_owner")
    assert payee.room_terms(room, key) == ({"pb_owner"}, 150)
    assert payee.within_signed_fee(1_500_000, 150) == ""
    assert "more than the 1.50" in payee.within_signed_fee(1_500_001, 150)
    with pytest.raises(payee.PayeeError, match="did not sign"):
        payee.room_terms({**room, "entry_fee_cents": 900}, key)          # the relay raised the fee
    with pytest.raises(payee.PayeeError, match="did not sign"):
        payee.room_terms(room, identity.get_public_key_hex("someone_else_pb"))
    assert payee.room_terms(None, "") == (set(), None)                   # not shown to this payer: as before
    # A paid room's card, shown before joining, leaves out the game it is tied to — still its creator's.
    card = {k: v for k, v in room.items() if k != "linked_session_id"} | {"card_only": True}
    assert payee.room_terms(card, key) == ({"pb_owner"}, 150)


def test_an_invitee_may_read_the_signed_terms_of_the_room_it_is_asked_into(client):
    for a in ("pb_host", "pb_invitee", "pb_stranger"):
        _register_keyed(client, a)
    room_id = client.post("/rooms", json=_signed_room("pb_host", {
        "name": "pb invite room", "room_type": "private", "gate_type": "invite"}), headers=_as("pb_host")).json()["room_id"]
    assert client.get(f"/rooms/{room_id}", headers=_as("pb_invitee")).status_code == 403
    assert client.post(f"/rooms/{room_id}/invite", json=_signed_invite("pb_host", room_id, {
        "invitee_id": "pb_invitee", "invited_by": "pb_host"}), headers=_as("pb_host")).status_code == 200
    seen = client.get(f"/rooms/{room_id}", headers=_as("pb_invitee")).json()
    assert seen["card_only"] and [a["act"] for a in seen["signed_acts"]] == ["create"]
    assert "members" not in seen
    assert payee.room_terms(seen, identity.get_public_key_hex("pb_host"))[0] == {"pb_host"}
    assert client.get(f"/rooms/{room_id}", headers=_as("pb_stranger")).status_code == 403


def test_the_payee_must_be_one_of_the_agents_expected():
    addr = "0x" + "ab" * 20
    from conftest import payee_for
    statement = payee_for("pb_relays_own", evm=addr)
    assert payee.check(statement, "evm", addr, expected_agents={"pb_relays_own"})
    with pytest.raises(payee.PayeeError, match="not pb_creator"):
        payee.check(statement, "evm", addr, expected_agents={"pb_creator"})


def _creator_relay(creator: str, fee_cents: int, asked_atomic: int, payee_name: str = "") -> _FakeHttpx:
    """The relay as the MCP server sees it: a room `creator` signed with `fee_cents`, and a
    402 asking `asked_atomic` for the payee `payee_name` (default the creator), whose wallet it signed."""
    pub = identity.get_public_key_hex(creator)
    payee_name = payee_name or creator
    payee_pub = identity.get_public_key_hex(payee_name)
    sid, at, pay_to = wire.stable_id_for(payee_pub), datetime.now(UTC).isoformat(), Account.create().address
    statement = {"agent_id": payee_name, "stable_id": sid, "public_key_hex": payee_pub, "key_log": [],
                 "wallets": {"evm": {"address": pay_to, "at": at, "signature": identity.sign(
                     wire.signed_payout_wallet_payload(sid, "evm", pay_to, at), payee_name)}}}
    settings = {"name": "fee room", "description": "", "room_type": "private", "gate_type": "paid",
                "entry_fee_cents": fee_cents, "currency": "USD", "max_members": None, "encrypted": False,
                "linked_session_id": None, "access_duration_days": None}
    nonce = wire.new_message_nonce()
    signed = wire.signed_room_payload(nonce, *settings.values())
    room = {"room_id": "r1", "creator_id": creator, **settings, "signed_acts": [
        {"act": "create", "at": at, "payload": signed, "signature": identity.sign(signed, creator)}]}
    challenge = {"x402": {"x402Version": 2, "resource": {"url": "http://relay/rooms/r1/join"}, "accepts": [{
        "scheme": "exact", "network": NET, "amount": str(asked_atomic), "payTo": pay_to,
        "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "maxTimeoutSeconds": 300,
        "extra": {"name": "USDC", "version": "2"}}]}, "payee": statement}

    class Relay(_FakeHttpx):
        def get(self, url, headers=None, **kwargs):
            if url.endswith("/rooms/r1"):
                return _Resp(200, room)
            for who, key in ((creator, pub), (payee_name, payee_pub)):
                if url.endswith(f"/agents/{who}"):
                    return _Resp(200, {"public_key_hex": key, "stable_id": wire.stable_id_for(key), "key_log": []})
            return _Resp(404, {})
    return Relay(challenge)


def _join_paying(relay: _FakeHttpx, payer: str) -> dict:
    from emporia import agent_wallet
    agent_wallet.generate(payer)
    with patch.object(mcp_server, "httpx", relay), \
         patch.object(mcp_server, "_dashboard_bearer_headers", return_value={"Authorization": "Bearer t"}):
        return mcp_server.join_room(room_id="r1", agent_id=payer, pay_with_x402=True)


def test_mcp_pays_a_room_fee_to_its_creator_within_the_signed_fee(wallet_env):  # noqa: F811
    relay = _creator_relay("pb_fee_owner", 100, 1_000_000)
    out = _join_paying(relay, "pb_fee_payer")
    assert out["success"] is True, out
    assert "PAYMENT-SIGNATURE" in relay.calls[-1]["headers"]


def test_mcp_does_not_pay_more_than_the_creator_signed(wallet_env):  # noqa: F811
    relay = _creator_relay("pb_fee_owner2", 100, 2_500_000)
    out = _join_paying(relay, "pb_fee_payer2")
    assert not out["success"] and "more than the 1.00" in out["error"], out
    assert not any("PAYMENT-SIGNATURE" in c["headers"] for c in relay.calls)


def test_mcp_does_not_pay_a_room_fee_to_anyone_but_its_creator(wallet_env):  # noqa: F811
    relay = _creator_relay("pb_real_owner", 100, 1_000_000, payee_name="pb_relays_agent")
    out = _join_paying(relay, "pb_fee_payer3")
    assert not out["success"] and "not pb_real_owner" in out["error"], out
    assert not any("PAYMENT-SIGNATURE" in c["headers"] for c in relay.calls)


def test_mcp_stakes_only_toward_another_player_seated_by_its_own_join(wallet_env):  # noqa: F811
    from conftest import payee_for

    from emporia import agent_wallet
    agent_wallet.generate("pb_buyer")
    offer = {"scheme": "emporia-escrow-v1", "payment_info": {"receiver": "0x" + "ee" * 20},
             "payee": payee_for("pb_seller", evm="0x" + "ee" * 20)}
    signed = []

    class Relay:
        calls: list[dict] = []

        def post(self, url, headers=None, **kw):
            return _Resp(402, {"escrow": offer})

        def get(self, url, headers=None, **kw):
            if url.endswith("/encryption-keys"):   # the relay names nobody but the buyer as seated
                return _Resp(200, {"creator": "pb_buyer", "keys": {}, "joins": {}, "leaves": []})
            return _Resp(200, {"payment_rules": {"price": "1.00"}})

    with patch.object(mcp_server, "httpx", Relay()), \
         patch.object(mcp_server, "_dashboard_bearer_headers", return_value={}), \
         patch.object(agent_wallet, "sign_escrow", lambda *a, **k: signed.append(1) or "x"):
        out = mcp_server.fund_session(session_id="s1", agent_id="pb_buyer", pay_with_x402=True)
    # a game with no signed creation is refused before anyone is looked for (review of P280)
    assert not out["success"] and "no creation signed" in out["error"], out
    assert not signed


def test_mcp_pays_a_topic_fee_only_to_its_creator_within_the_signed_fee(wallet_env):  # noqa: F811
    """P278: as for rooms — the topic's creator, whose signed settings check out, at most the fee it signed."""
    def relay_for(creator: str, fee: int, asked: int, payee_name: str = "") -> _FakeHttpx:
        base = _creator_relay(creator, fee, asked, payee_name)       # its 402 and its agents' keys
        nonce = wire.new_message_nonce()
        signed = wire.signed_agora_topic_payload(nonce, "club", "Club", "", "private", "paid_invite", fee, [], None)
        topic = {"slug": "club", "name": "Club", "description": "", "visibility": "private",
                 "gate_type": "paid_invite", "entry_fee_cents": fee, "flair_options": [], "access_duration_days": None,
                 "encrypted": False, "creator_id": creator, "card_only": True, "signed_acts": [
                     {"act": "create", "at": datetime.now(UTC).isoformat(), "payload": signed,
                      "signature": identity.sign(signed, creator)}]}
        rooms_get = base.get

        def get(url, headers=None, **kwargs):
            return _Resp(200, topic) if url.endswith("/agoras/topics/club") else rooms_get(url, headers, **kwargs)
        base.get = get
        return base

    from emporia import agent_wallet

    def subscribe(relay, payer):
        agent_wallet.generate(payer)
        with patch.object(mcp_server, "httpx", relay), \
             patch.object(mcp_server, "_dashboard_bearer_headers", return_value={"Authorization": "Bearer t"}):
            return mcp_server.subscribe_agora_topic(topic_slug="club", agent_id=payer, pay_with_x402=True)

    ok = relay_for("pt_owner", 100, 1_000_000)
    assert subscribe(ok, "pt_payer1").get("success") is not False
    assert "PAYMENT-SIGNATURE" in ok.calls[-1]["headers"]
    for relay, why in ((relay_for("pt_owner2", 100, 2_500_000), "more than the 1.00"),
                       (relay_for("pt_owner3", 100, 1_000_000, payee_name="pt_relays_agent"), "not pt_owner3")):
        out = subscribe(relay, f"pt_payer_{why[:4].strip()}")
        assert why in str(out), out
        assert not any("PAYMENT-SIGNATURE" in c["headers"] for c in relay.calls)


def test_review_a_room_or_topic_without_its_signed_creation_is_not_paid():
    """Review of P275/P278: a relay that strips the creation would otherwise make the fee its own word."""
    from emporia import payee
    with pytest.raises(payee.PayeeError, match="no creation signed"):
        payee.room_terms({"room_id": "r1", "creator_id": "someone", "entry_fee_cents": 500, "signed_acts": []}, "")
    with pytest.raises(payee.PayeeError, match="no creation signed"):
        payee.topic_terms({"slug": "t", "creator_id": "someone", "entry_fee_cents": 500, "signed_acts": []}, "")


def test_review_tempo_pays_no_more_than_the_signed_fee():
    """Review of P275: the Tempo rail is held to the fee its payee signed, as x402 is."""
    from emporia import payee
    offer = {"tempo": {"payee": {}, "request": {"amount": "5000000", "recipient": "0x" + "ab" * 20}}}
    with patch.object(mcp_server, "_check_payee_pinned", lambda *a, **k: None), \
         pytest.raises(payee.PayeeError, match="more than the 1.00"):
        mcp_server._tempo_paid_retry(_Resp(402, offer), "a", "post", "http://relay/rooms/r1/join", max_cents=100)
