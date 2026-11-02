"""P262: an agent's payout wallets are its own signed word, checked by the payer."""
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from conftest import give_wallet, payee_for, signed_wallet_body
from emporia import identity, payee, wire
from emporia.relay_server import AGENT_REGISTRY, app

WALLET = "0x" + "ab" * 20


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51216))


def _agent(client, name):
    client.post("/agents/register", json={"agent_id": name, "public_key_hex": identity.get_public_key_hex(name)})
    return name


def test_an_unsigned_or_misdated_wallet_is_refused(client):
    a = _agent(client, "pw_owner")
    hdr = {"x-emporia-agent-id": a}
    assert client.post(f"/agents/{a}/x402-wallet", json={"network_family": "evm", "address": WALLET},
                       headers=hdr).status_code == 422                                   # no at, no signature
    forged = {**signed_wallet_body(a, "evm", WALLET), "address": "0x" + "cd" * 20}  # signed one, sent another
    assert client.post(f"/agents/{a}/x402-wallet", json=forged, headers=hdr).status_code == 403
    by_other = signed_wallet_body(a, "evm", WALLET, key_profile="pw_someone_else")
    assert client.post(f"/agents/{a}/x402-wallet", json=by_other, headers=hdr).status_code == 403
    stale = signed_wallet_body(a, "evm", WALLET)
    stale["at"] = "2020-01-01T00:00:00+00:00"
    assert client.post(f"/agents/{a}/x402-wallet", json=stale, headers=hdr).status_code == 400
    assert AGENT_REGISTRY.get_x402_pay_to(a, "evm") is None


def test_a_signed_wallet_is_stored_and_served_as_the_payee(client):
    a = _agent(client, "pw_signed")
    r = client.post(f"/agents/{a}/x402-wallet", json=signed_wallet_body(a, "evm", WALLET),
                    headers={"x-emporia-agent-id": a})
    assert r.status_code == 200, r.text
    statement = AGENT_REGISTRY.payee_statement(a)
    assert payee.check(statement, "evm", WALLET).startswith(f"{a} (")
    assert payee.check(statement, "evm", WALLET.upper().replace("0X", "0x"))     # EVM addresses: any case


def test_a_payer_refuses_a_wallet_the_relay_swapped_in(client):
    """The relay's own row says pay the attacker; the payee's signature says otherwise."""
    a = _agent(client, "pw_victim")
    give_wallet(a, "evm", WALLET)
    import json

    from emporia import relay_server as rs
    with rs.DB_LOCK, rs.get_db() as conn:          # what a hostile relay, or its database, can do
        conn.execute("UPDATE authorized_agents SET x402_pay_to_json = ? WHERE agent_id = ?",
                     (json.dumps({"evm": "0x" + "66" * 20}), a))
    statement = AGENT_REGISTRY.payee_statement(a)
    with pytest.raises(payee.PayeeError, match="not the evm wallet its payee signed"):
        payee.check(statement, "evm", "0x" + "66" * 20)
    # and rewriting the signed statement too needs the payee's key
    statement["wallets"]["evm"]["address"] = "0x" + "66" * 20
    with pytest.raises(payee.PayeeError, match="signature"):
        payee.check(statement, "evm", "0x" + "66" * 20)


def test_no_statement_a_made_up_key_or_another_agent_is_refused():
    with pytest.raises(payee.PayeeError, match="does not say who is paid"):
        payee.check(None, "evm", WALLET)
    real = payee_for(evm=WALLET)
    made_up = {**real, "public_key_hex": payee_for(evm=WALLET)["public_key_hex"]}   # a key not of this stable id
    with pytest.raises(payee.PayeeError, match="key"):
        payee.check(made_up, "evm", WALLET)
    with pytest.raises(payee.PayeeError, match="different agent"):
        payee.check(real, "evm", WALLET, expected_stable_id="00" * 32)


def test_a_known_name_cannot_be_given_to_another_agent(tmp_path, monkeypatch):
    """The payer pins who a payee is on a relay; a relay swapping in an agent of
    its own under that name is refused."""
    monkeypatch.setattr(identity, "KEY_DIR", tmp_path)
    first = payee_for("pw_seller", evm=WALLET)
    assert payee.check_pinned(first, "evm", WALLET, "http://relay.example")
    impostor = payee_for("pw_seller", evm=WALLET)        # same name, a different agent
    with pytest.raises(payee.PayeeError, match="not the agent first seen"):
        payee.check_pinned(impostor, "evm", WALLET, "http://relay.example")
    assert payee.check_pinned(impostor, "evm", WALLET, "http://other.example")   # another relay, another name


def test_a_wallet_signed_before_a_key_rotation_still_pays():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    old, new = ed25519.Ed25519PrivateKey.generate(), ed25519.Ed25519PrivateKey.generate()
    old_hex, new_hex = (k.public_key().public_bytes_raw().hex() for k in (old, new))
    sid = wire.stable_id_for(old_hex)
    signed_at = datetime(2026, 11, 1, tzinfo=UTC).isoformat()
    rotated_at = datetime(2026, 11, 2, tzinfo=UTC).isoformat()
    step = {"by": "key", "old": old_hex, "new": new_hex, "at": rotated_at,
            "signature": identity.sign_payload(old, wire.signed_key_rotation_payload(sid, old_hex, new_hex, rotated_at))}
    statement = {"agent_id": "pw_rotated", "stable_id": sid, "public_key_hex": new_hex, "key_log": [step],
                 "wallets": {"evm": {"address": WALLET, "at": signed_at, "signature": identity.sign_payload(
                     old, wire.signed_payout_wallet_payload(sid, "evm", WALLET, signed_at))}}}
    assert payee.check(statement, "evm", WALLET)
    # the new key signing for a time the old one held is not the payee's word
    statement["wallets"]["evm"]["signature"] = identity.sign_payload(
        new, wire.signed_payout_wallet_payload(sid, "evm", WALLET, signed_at))
    with pytest.raises(payee.PayeeError, match="signature"):
        payee.check(statement, "evm", WALLET)

