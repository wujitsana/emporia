"""The signed ERC-8004 link record (`emporia:erc8004-link:v1`): its exact bytes, a signature over them, the
shape rules, and its place in the signing table (a person's kind; unlink is the ordinary signed removal)."""
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from emporia import identity, signing, wire

STABLE = "ab" * 32
REGISTRY = "eip155:84532:0x8004a818bfb912233c491871b3d84c89a494bd9e"
SIGNER = "0x" + "11" * 20
WALLET_SIG = "0x" + "22" * 65
AT = "2025-10-09T08:53:20+00:00"

# Vector: the canonical bytes (sorted keys, no spaces) and the base64url Ed25519 signature of a fixed key.
VECTOR_BYTES = (
    '{"agent_id":"42","agent_registry":"eip155:84532:0x8004a818bfb912233c491871b3d84c89a494bd9e",'
    '"at":"2025-10-09T08:53:20+00:00","issued_at":1760000000,"signer":"0x1111111111111111111111111111111111111111",'
    '"stable_id":"' + STABLE + '","type":"emporia:erc8004-link:v1","wallet_sig":"0x' + "22" * 65 + '"}')
VECTOR_SIGNATURE = "s3NsAx0Kr5zJDeOByPYzq6R1Zc6TYCmzKHnyl-2Vrjw1iu44mrTJvkDYF9mAVxWOoSkHZAe09e612Pb-GLtGCA"


def _payload(**over):
    p = wire.signed_erc8004_link_payload(STABLE, REGISTRY, "42", SIGNER, 1760000000, WALLET_SIG, AT)
    p.update(over)
    return p


def _key():
    return ed25519.Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"emporia test vector chain-link").digest())


def test_the_payload_bytes_and_signature_are_fixed():
    p = _payload()
    assert json.dumps(p, sort_keys=True, separators=(",", ":")) == VECTOR_BYTES
    assert identity.sign_payload(_key(), p) == VECTOR_SIGNATURE
    pub = _key().public_key().public_bytes_raw().hex()
    assert identity.verify(p, VECTOR_SIGNATURE, pub)
    assert not identity.verify(_payload(agent_id="43"), VECTOR_SIGNATURE, pub)
    assert not identity.verify(_payload(signer="0x" + "12" * 20), VECTOR_SIGNATURE, pub)


def test_the_agent_id_is_a_decimal_string_never_a_number():
    assert _payload()["agent_id"] == "42"
    assert wire.erc8004_link_problem(_payload(agent_id=42))
    assert wire.erc8004_link_problem(_payload(agent_id="0x2a"))
    assert wire.erc8004_link_problem(_payload(agent_id=str(1 << 256)))
    assert wire.erc8004_link_problem(_payload(agent_id=str((1 << 256) - 1))) == ""


@pytest.mark.parametrize("over", [
    {"agent_registry": "eip155:84532:0x8004A818BFB912233c491871b3d84c89A494BD9e"},     # not lowercase
    {"agent_registry": "solana:abc:0x" + "0" * 40}, {"agent_registry": "eip155:84532"},
    {"signer": "0x1234"}, {"stable_id": "xyz"}, {"stable_id": "AB" * 32},
    {"issued_at": "1760000000"}, {"issued_at": True}, {"issued_at": 0}, {"issued_at": 1 << 64},
    {"wallet_sig": ""}, {"wallet_sig": "0xabc"}, {"wallet_sig": "0x" + "zz" * 65},
    {"wallet_sig": "0x" + "aa" * 4097},
])
def test_a_malformed_record_is_refused(over):
    assert wire.erc8004_link_problem(_payload(**over)), over


def test_a_good_record_and_the_largest_signature_pass():
    assert wire.erc8004_link_problem(_payload()) == ""
    assert wire.erc8004_link_problem(_payload(wallet_sig="0x" + "aa" * 4096)) == ""


def test_a_link_is_a_persons_kind_and_an_agents_tools_cannot_sign_it():
    assert signing.KINDS["erc8004-link"].person
    assert signing.payload("erc8004-link", STABLE, REGISTRY, "42", SIGNER, 1760000000, WALLET_SIG, AT) == _payload()
    with pytest.raises(signing.Refused, match="its person's to sign"):
        signing.check("erc8004-link", _payload(), signing.Context("a"))


def test_unlinking_is_the_existing_signed_removal_naming_the_link():
    item = wire.chain_link_item(STABLE, REGISTRY, "42")
    assert item == f"{STABLE}/{REGISTRY}/42"
    p = wire.signed_delete_payload(wire.CHAIN_LINK_REMOVAL, item, STABLE, AT)
    assert p == {"type": "emporia:delete:v3", "kind": "chain-link", "item": item, "stable_id": STABLE, "at": AT}


@pytest.mark.parametrize("bad", ["042", "00", "+42", "-1", "4 2", "٤٢", "1" * 79])
def test_a_non_canonical_agent_id_is_refused_and_cannot_be_built(bad):
    assert wire.erc8004_link_problem(_payload(agent_id=bad))
    with pytest.raises(ValueError):
        wire.signed_erc8004_link_payload(STABLE, REGISTRY, bad, SIGNER, 1760000000, WALLET_SIG, AT)


def test_zero_and_a_plain_number_are_canonical():
    assert wire.erc8004_link_problem(_payload(agent_id="0")) == ""
    assert wire.erc8004_link_problem(_payload(agent_id="1" + "0" * 76)) == ""
