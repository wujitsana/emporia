"""Signer phase 2 (P293): the back ends behind the signer, by kind and capability.

Every back end other than the local files is tested against a fake here — a
fake KMS client, fake Privy and CDP APIs on an `httpx.MockTransport`, fake `op`
and `bw` commands on PATH. No network, no account, no credential.
"""
import base64
import hashlib
import json
import os
import uuid
from unittest.mock import patch

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, x25519
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
from eth_account import Account
from eth_account.messages import encode_typed_data
from fastapi.testclient import TestClient

from emporia import agent_wallet, crypto_box, identity, signer, signer_backends as sb, signer_client, signing
from tests.test_wallet_setup import BASE, _challenge

AGENT = "be_agent"


# ─── fakes ────────────────────────────────────────────────────────────────────

class FakeKms:
    """KMS's CreateKey, GetPublicKey and Sign, with keys in memory — and its limits."""

    def __init__(self):
        self.keys, self.calls = {}, []

    def create_key(self, KeySpec, KeyUsage, Description="", Tags=()):  # noqa: N803 — boto3's names
        assert KeyUsage == "SIGN_VERIFY"
        priv = {"ECC_SECG_P256K1": lambda: ec.generate_private_key(ec.SECP256K1()),
                "ECC_NIST_EDWARDS25519": ed25519.Ed25519PrivateKey.generate}[KeySpec]()
        kid = str(uuid.uuid4())
        self.keys[f"arn:aws:kms:us-east-1:000000000000:key/{kid}"] = priv
        return {"KeyMetadata": {"KeyId": kid, "Arn": f"arn:aws:kms:us-east-1:000000000000:key/{kid}"}}

    def get_public_key(self, KeyId):  # noqa: N803
        return {"PublicKey": self.keys[KeyId].public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)}

    def sign(self, KeyId, Message, MessageType, SigningAlgorithm):  # noqa: N803
        self.calls.append((MessageType, SigningAlgorithm, len(Message)))
        assert 1 <= len(Message) <= 4096
        priv = self.keys[KeyId]
        if isinstance(priv, ed25519.Ed25519PrivateKey):
            assert (MessageType, SigningAlgorithm) == ("RAW", "ED25519_SHA_512")
            return {"Signature": priv.sign(Message)}
        assert (MessageType, SigningAlgorithm, len(Message)) == ("DIGEST", "ECDSA_SHA_256", 32)
        return {"Signature": priv.sign(Message, ec.ECDSA(Prehashed(hashes.SHA256())))}   # DER, any s


def _full_message_from(typed: dict) -> dict:
    return {"domain": typed["domain"], "types": typed["types"], "message": typed["message"],
            "primaryType": typed.get("primary_type") or typed["primaryType"]}


class FakePrivy:
    """Privy's wallet API: wallets, `eth_signTypedData_v4` and Solana `signTransaction`,
    a policy (a cap on a transfer's value), and the request signature checked."""

    def __init__(self, auth_pub=None, max_value=None):
        self.wallets, self.auth_pub, self.max_value, self.seen = {}, auth_pub, max_value, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        app_id = request.headers["privy-app-id"]
        assert request.headers["authorization"] == "Basic " + base64.b64encode(f"{app_id}:s3cret".encode()).decode()
        body = json.loads(request.content) if request.content else None
        if self.auth_pub is not None and request.method != "GET":
            payload = {"version": 1, "method": request.method, "url": str(request.url), "body": body,
                       "headers": {"privy-app-id": app_id}}
            self.auth_pub.verify(base64.b64decode(request.headers["privy-authorization-signature"]),
                                 sb.canonical_json(payload).encode(), ec.ECDSA(hashes.SHA256()))
        path = request.url.path
        self.seen.append((request.method, path, body))
        if request.method == "POST" and path == "/v1/wallets":
            wid = "w" + uuid.uuid4().hex[:12]
            if body["chain_type"] == "ethereum":
                acct = Account.create()
                self.wallets[wid] = ("evm", acct)
                return httpx.Response(200, json={"id": wid, "address": acct.address.lower(),
                                                 "chain_type": "ethereum"})
            from solders.keypair import Keypair
            kp = Keypair()
            self.wallets[wid] = ("svm", kp)
            return httpx.Response(200, json={"id": wid, "address": str(kp.pubkey()), "chain_type": "solana"})
        wid = path.split("/")[3]
        family, key = self.wallets[wid]
        if request.method == "GET":
            return httpx.Response(200, json={"id": wid, "address": key.address if family == "evm" else str(key.pubkey())})
        if body["method"] == "eth_signTypedData_v4":
            msg = _full_message_from(body["params"]["typed_data"])
            if self.max_value is not None and int(msg["message"]["value"]) > self.max_value:
                return httpx.Response(400, json={"error": "Policy violation: value above the limit"})
            sig = key.sign_typed_data(full_message=msg).signature
            return httpx.Response(200, json={"method": body["method"],
                                             "data": {"signature": "0x" + bytes(sig).hex(), "encoding": "hex"}})
        if body["method"] == "signTransaction":
            from solders.transaction import VersionedTransaction
            from solders.message import to_bytes_versioned
            tx = VersionedTransaction.from_bytes(base64.b64decode(body["params"]["transaction"]))
            signers = list(tx.message.account_keys[:tx.message.header.num_required_signatures])
            sigs = list(tx.signatures)
            sigs[signers.index(key.pubkey())] = key.sign_message(to_bytes_versioned(tx.message))
            signed = VersionedTransaction.populate(tx.message, sigs)        # partly: the fee payer's is not ours
            return httpx.Response(200, json={"method": "signTransaction", "data": {
                "signed_transaction": base64.b64encode(bytes(signed)).decode(), "encoding": "base64"}})
        return httpx.Response(400, json={"error": "unknown method"})


def _jwt_parts(token):
    h, c, s = token.split(".")
    pad = lambda x: base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))  # noqa: E731
    return json.loads(pad(h)), json.loads(pad(c)), pad(s), f"{h}.{c}".encode()


class FakeCdp:
    """CDP's v2 EVM accounts: create, and sign typed data — the bearer JWT
    (EdDSA) and the wallet JWT (ES256, over the body's hash) both checked."""

    def __init__(self, api_pub, wallet_pub):
        self.api_pub, self.wallet_pub, self.accounts = api_pub, wallet_pub, {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        uri = f"{request.method} {request.url.host}{request.url.path}"
        header, claims, sig, signed = _jwt_parts(request.headers["authorization"].removeprefix("Bearer "))
        assert header["alg"] == "EdDSA" and claims["uri"] == uri and claims["iss"] == "cdp"
        self.api_pub.verify(sig, signed)
        body = json.loads(request.content) if request.content else None
        if request.method == "POST":
            wh, wc, ws, wsigned = _jwt_parts(request.headers["x-wallet-auth"])
            assert wh["alg"] == "ES256" and wc["uris"] == [uri]
            assert wc["reqHash"] == hashlib.sha256(sb.canonical_json(body).encode()).hexdigest()
            from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
            self.wallet_pub.verify(encode_dss_signature(int.from_bytes(ws[:32], "big"), int.from_bytes(ws[32:], "big")),
                                   wsigned, ec.ECDSA(hashes.SHA256()))
        if request.url.path == "/platform/v2/evm/accounts":
            acct = Account.create()
            self.accounts[acct.address] = acct
            return httpx.Response(201, json={"address": acct.address, "name": body.get("name")})
        address = request.url.path.split("/")[5]
        sig = self.accounts[address].sign_typed_data(full_message=_full_message_from(body)).signature
        return httpx.Response(200, json={"signature": "0x" + bytes(sig).hex()})


# ─── a signer, an agent behind it, the back ends registered ──────────────────

@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("EMPORIA_SIGNER_DIR", str(tmp_path / "signer"))
    app = TestClient(signer.app, base_url="http://signer")
    monkeypatch.setattr(signer_client.Client, "_http", lambda self: app)
    monkeypatch.setattr(signer, "_OPENS", {})
    signer_client._CACHE.clear()
    sent = []
    monkeypatch.setattr("emporia.signer_alerts.notify", lambda a, e, d: sent.append((a, e, d)))
    keys = tmp_path / "agent-keys"
    keys.mkdir()
    kms = FakeKms()
    signer.save_backend("kms1", "aws-kms", {"region": "us-east-1"}, {})
    monkeypatch.setitem(sb.OVERRIDES, "kms1", sb.KmsBackend("kms1", {"region": "us-east-1"}, client=kms))
    signer.save_policy(AGENT, {"EMPORIA_X402_PAY_NETWORKS": BASE, "EMPORIA_X402_MAX_PAYMENT_CENTS": "500",
                               "EMPORIA_X402_MAX_DAILY_CENTS": "2000", "EMPORIA_RELAY_URL": "https://home.example"})
    token = signer.new_agent_token(AGENT)
    agent_side = {"EMPORIA_SIGNER_URL": "unix:///x/signer.sock", "EMPORIA_SIGNER_TOKEN": token,
                  "EMPORIA_AGENT_ID": AGENT, "EMPORIA_RELAY_URL": "https://home.example"}
    with patch.object(identity, "KEY_DIR", keys), patch.dict(os.environ, agent_side):
        yield {"app": app, "kms": kms, "keys": keys, "sent": sent, "tmp": tmp_path}


def _marker(keys, made):
    (keys / f"{AGENT}.signer.pub").write_text(made["identity"] + "\n")
    (keys / f"{AGENT}.x25519.signer.pub").write_text(made["encryption"] + "\n")


def _authorization(header: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))["payload"]


def _x402_signer_of(header: str, req: dict) -> str:
    """The address that signed an x402 PAYMENT-SIGNATURE, from the EIP-3009 it authorizes."""
    p = _authorization(header)
    a = p["authorization"]
    msg = encode_typed_data(full_message={
        "types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                   {"name": "chainId", "type": "uint256"},
                                   {"name": "verifyingContract", "type": "address"}],
                  "TransferWithAuthorization": [{"name": n, "type": t} for n, t in (
                      ("from", "address"), ("to", "address"), ("value", "uint256"), ("validAfter", "uint256"),
                      ("validBefore", "uint256"), ("nonce", "bytes32"))]},
        "primaryType": "TransferWithAuthorization",
        "domain": {"name": "USDC", "version": "2", "chainId": int(req["network"].split(":")[1]),
                   "verifyingContract": req["asset"]},
        "message": {"from": a["from"], "to": a["to"], "value": int(a["value"]), "validAfter": int(a["validAfter"]),
                    "validBefore": int(a["validBefore"]), "nonce": bytes.fromhex(a["nonce"][2:])}})
    return Account.recover_message(msg, signature=p["signature"])


def _challenge_for(cents):
    body = _challenge(BASE)
    body["x402"]["accepts"][0]["amount"] = str(cents * 10_000)
    return body


# ─── the interface ────────────────────────────────────────────────────────────

def test_every_back_end_declares_what_it_does_and_whether_it_was_tried_live():
    for type_ in sb.TYPES:
        b = sb.make_backend("x", type_)
        assert b.capabilities <= set(sb.CAPABILITIES) and b.type == type_
        assert b.live_tested is (type_ == "local")
    assert sb.make_backend("x", "local").capabilities == set(sb.CAPABILITIES)
    assert "open" not in sb.make_backend("x", "aws-kms").capabilities         # providers sign, they don't decrypt
    assert sb.make_backend("x", "privy").capabilities == {"evm-typed-data", "solana", "make-keys"}
    assert sb.make_backend("x", "cdp").capabilities == {"evm-typed-data", "make-keys"}
    assert "make-keys" not in sb.make_backend("x", "vault").capabilities


def test_the_x402_signature_is_the_same_through_the_new_signer_path():
    """The payer now hands EIP-712 to a signer object; a key here signs exactly as before."""
    from emporia import x402_payer
    acct = Account.create()
    req = _challenge(BASE)["x402"]["accepts"][0]
    out = x402_payer.build_payment_payload(req, "0x" + bytes(acct.key).hex(), now=1_800_000_000)
    old = Account.sign_typed_data(
        bytes(acct.key), domain_data={"name": "USDC", "version": "2", "chainId": 84532, "verifyingContract": req["asset"]},
        message_types=x402_payer._TRANSFER_WITH_AUTHORIZATION_TYPES,
        message_data={"from": acct.address, "to": req["payTo"], "value": 250000, "validAfter": 1_800_000_000 - 60,
                      "validBefore": 1_800_000_060,
                      "nonce": bytes.fromhex(out["payload"]["authorization"]["nonce"][2:])})
    assert out["payload"]["signature"] == "0x" + bytes(old.signature).hex()


# ─── AWS KMS ──────────────────────────────────────────────────────────────────

def test_kms_wallet_pays_x402_under_the_signers_caps(setup):
    made = signer.make_agent_keys(AGENT, {"evm": "kms1"})
    _marker(setup["keys"], made)
    assert made["evm"] == agent_wallet.address(AGENT)                       # the agent sees the KMS wallet
    header = agent_wallet.sign_exact(AGENT, _challenge_for(250))
    assert _x402_signer_of(header, _challenge(BASE)["x402"]["accepts"][0]) == made["evm"]
    assert ("DIGEST", "ECDSA_SHA_256", 32) in setup["kms"].calls
    with pytest.raises(agent_wallet.AgentWalletError, match="per-payment cap"):
        agent_wallet.sign_exact(AGENT, _challenge_for(600))                # the signer's rule, whichever back end
    assert not list(setup["keys"].glob("*.secp256k1"))


def test_kms_signs_escrow_holds_and_transactions_with_low_s_and_the_right_recovery_id(setup):
    from emporia import escrow
    b = sb.OVERRIDES["kms1"]
    ref = b.make(AGENT, "evm")
    address = b.public(AGENT, "evm", ref)
    msg = escrow.receive_with_authorization_message(
        escrow.PaymentInfo.from_json({"operator": "0x" + "33" * 20, "payer": address, "receiver": "0x" + "44" * 20,
                                      "token": "0x" + "22" * 20, "max_amount": 5, "pre_approval_expiry": 2_000_000_000,
                                      "authorization_expiry": 2_000_000_100, "refund_expiry": 2_000_000_200,
                                      "min_fee_bps": 0, "max_fee_bps": 100, "fee_receiver": "0x" + "55" * 20,
                                      "salt": 7}),
        chain_id=84532, escrow="0x" + "66" * 20, collector="0x" + "77" * 20, token_name="USDC", token_version="2")
    for _ in range(6):                                  # high-s DER signatures come up about half the time
        sig = b.sign_typed_data(AGENT, msg, ref)
        assert int.from_bytes(sig[32:64], "big") <= sb._SECP256K1_N // 2
        assert sb.recover_typed_data(msg, sig) == address
    tx = {"type": 2, "chainId": 42431, "to": "0x" + "11" * 20, "value": 0, "data": "0x1234", "gas": 50000,
          "maxFeePerGas": 10**9, "maxPriorityFeePerGas": 10**8, "nonce": 3}
    assert Account.recover_transaction(b.sign_transaction(AGENT, tx, ref)) == address
    with pytest.raises(sb.Unsupported, match="type 2"):
        b.sign_transaction(AGENT, {**tx, "type": 0}, ref)


def test_kms_holds_the_identity_and_signs_by_kind_up_to_4096_bytes(setup):
    made = signer.make_agent_keys(AGENT, {"identity": "kms1"})
    _marker(setup["keys"], made)
    p, sig = signing.signed(AGENT, "room-message", "room-1", "hello", "chat", "n" * 32)
    assert identity.verify(p, sig, made["identity"])                        # plain Ed25519: relays verify as ever
    assert ("RAW", "ED25519_SHA_512", len(json.dumps(p, sort_keys=True, separators=(",", ":")))) in setup["kms"].calls
    with pytest.raises(signing.Refused, match="4096"):
        signing.sign(AGENT, "room-message", "room-1", "x" * 5000, "chat", "n" * 32)
    with pytest.raises(signing.Refused, match="not signed"):               # the rules before any back end
        signing.sign(AGENT, "payout-wallet", "sid", "evm", "0x" + "ee" * 20, "2026-11-04T00:00:00+00:00")


def test_kms_cannot_hold_or_make_an_encryption_key(setup):
    with pytest.raises(sb.Unsupported, match="cannot hold the encryption key"):
        signer.set_route(AGENT, "encryption", "kms1", "arn:x")
    with pytest.raises(sb.Unsupported):
        signer.make_agent_keys(AGENT, {"encryption": "kms1"})
    assert not signer.holds(AGENT, "identity")                             # nothing half-made


def test_kms_solana_key_signs_a_solana_message(setup):
    pytest.importorskip("solders")
    made = signer.make_agent_keys(AGENT, {"svm": "kms1"})
    from solders.pubkey import Pubkey
    s = signer.Wallet(AGENT).svm()
    assert str(s.pubkey()) == made["svm"] == str(Pubkey.from_string(made["svm"]))
    sig = s.sign_message(b"\x80a solana message")
    ed25519.Ed25519PublicKey.from_public_bytes(bytes(s.pubkey())).verify(bytes(sig), b"\x80a solana message")


def test_without_boto3_the_kms_back_end_says_how_to_get_it(monkeypatch):
    import builtins
    real = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", lambda n, *a, **k: (_ for _ in ()).throw(ImportError(n))
                        if n == "boto3" else real(n, *a, **k))
    with pytest.raises(sb.BackendError, match=r"emporia\[kms\]"):
        sb.KmsBackend("k", {"region": "us-east-1"}).public("a", "evm", "arn:x")


# ─── Privy ────────────────────────────────────────────────────────────────────

def _privy(setup, monkeypatch, **kw):
    auth = ec.generate_private_key(ec.SECP256R1())
    der = auth.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    fake = FakePrivy(auth_pub=auth.public_key(), **kw)
    signer.save_backend("privy1", "privy", {"app_id": "app-1"},
                        {"app_secret": "s3cret", "authorization_key": "wallet-auth:" + base64.b64encode(der).decode()})
    b = signer.get_backend("privy1")
    monkeypatch.setitem(sb.OVERRIDES, "privy1", sb.PrivyBackend("privy1", b.settings, b.secrets,
                                                                 transport=httpx.MockTransport(fake)))
    return fake


def test_privy_signs_x402_typed_data_and_its_policy_is_a_second_limit(setup, monkeypatch):
    fake = _privy(setup, monkeypatch, max_value=300 * 10_000)
    made = signer.make_agent_keys(AGENT, {"evm": "privy1"})
    assert fake.seen[0][1] == "/v1/wallets" and fake.seen[0][2] == {"chain_type": "ethereum"}
    header = agent_wallet.sign_exact(AGENT, _challenge_for(250))
    assert _x402_signer_of(header, _challenge(BASE)["x402"]["accepts"][0]) == made["evm"]
    rpc = [b for m, p, b in fake.seen if p.endswith("/rpc")][-1]
    assert rpc["method"] == "eth_signTypedData_v4" and rpc["caip2"] == BASE
    assert rpc["params"]["typed_data"]["primary_type"] == "TransferWithAuthorization"
    with pytest.raises(agent_wallet.AgentWalletError, match="Policy violation"):
        agent_wallet.sign_exact(AGENT, _challenge_for(400))                # under our cap, over Privy's


def test_privy_refuses_what_it_cannot_and_a_wrong_signature_is_caught(setup, monkeypatch):
    fake = _privy(setup, monkeypatch)
    b = sb.OVERRIDES["privy1"]
    with pytest.raises(sb.Unsupported):
        signer.set_route(AGENT, "identity", "privy1", "w1")                # no raw Ed25519 for Emporia's payloads
    ref = b.make(AGENT, "evm")
    with pytest.raises(sb.Unsupported, match="EVM transaction"):
        b.sign_transaction(AGENT, {"type": 2}, ref)                         # a Tempo payment: refused, said so
    other = Account.create()
    family, _ = fake.wallets[ref]
    fake.wallets[ref] = (family, other)                                     # a provider signing with another key
    b._addresses[ref] = Account.create().address
    msg = {"types": {"EIP712Domain": [{"name": "name", "type": "string"}], "M": [{"name": "a", "type": "uint256"}]},
           "primaryType": "M", "domain": {"name": "x"}, "message": {"a": 1}}
    with pytest.raises(sb.BackendError, match="not from the wallet"):
        b.sign_typed_data(AGENT, msg, ref)


def test_privy_signs_a_solana_payment_as_a_transaction(setup, monkeypatch):
    pytest.importorskip("solders")
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0, to_bytes_versioned
    from solders.pubkey import Pubkey
    fake = _privy(setup, monkeypatch)
    made = signer.make_agent_keys(AGENT, {"svm": "privy1"})
    s = signer.Wallet(AGENT).svm()
    fee_payer, me = Pubkey.new_unique(), Pubkey.from_string(made["svm"])
    ix = Instruction(Pubkey.new_unique(), b"\x01", [AccountMeta(me, is_signer=True, is_writable=False)])
    message = MessageV0.try_compile(fee_payer, [ix], [], Hash.default())
    sig = s.sign_message(to_bytes_versioned(message))
    ed25519.Ed25519PublicKey.from_public_bytes(bytes(me)).verify(bytes(sig), to_bytes_versioned(message))
    assert [b["method"] for _, p, b in fake.seen if p.endswith("/rpc")] == ["signTransaction"]


# ─── Coinbase Developer Platform ──────────────────────────────────────────────

def test_cdp_signs_x402_typed_data_with_both_tokens(setup, monkeypatch):
    api = ed25519.Ed25519PrivateKey.generate()
    wallet = ec.generate_private_key(ec.SECP256R1())
    fake = FakeCdp(api.public_key(), wallet.public_key())
    secrets_ = {"api_key_secret": base64.b64encode(api.private_bytes_raw() + api.public_key().public_bytes_raw()).decode(),
                "wallet_secret": base64.b64encode(wallet.private_bytes(
                    serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption())).decode()}
    signer.save_backend("cdp1", "cdp", {"api_key_id": "key-1"}, secrets_)
    monkeypatch.setitem(sb.OVERRIDES, "cdp1", sb.CdpBackend("cdp1", {"api_key_id": "key-1"}, secrets_,
                                                             transport=httpx.MockTransport(fake)))
    made = signer.make_agent_keys(AGENT, {"evm": "cdp1"})
    assert made["evm"] in fake.accounts and signer.routes(AGENT)["evm"]["ref"] == made["evm"]
    header = agent_wallet.sign_exact(AGENT, _challenge_for(250))
    assert _x402_signer_of(header, _challenge(BASE)["x402"]["accepts"][0]) == made["evm"]
    with pytest.raises(sb.Unsupported):
        sb.OVERRIDES["cdp1"].sign_ed25519(AGENT, "svm", b"x", made["evm"])


# ─── a password manager, read by the signer ───────────────────────────────────

def _fake_vault(tmp, keys: dict[str, bytes]):
    """`op` and `bw` that answer only with the token they expect, printing a hex key."""
    bin_ = tmp / "fakebin"
    bin_.mkdir(exist_ok=True)
    table = json.dumps({k: v.hex() for k, v in keys.items()})
    for tool, var in (("op", "OP_SERVICE_ACCOUNT_TOKEN"), ("bw", "BW_SESSION")):
        (bin_ / tool).write_text(f"""#!/usr/bin/env python3
import json, os, sys
if os.environ.get({var!r}) != "fake-token":
    sys.stderr.write("not signed in"); sys.exit(1)
print(json.loads({table!r})[sys.argv[-1]])
""")
        (bin_ / tool).chmod(0o755)
    return bin_


def test_the_signer_reads_keys_from_a_password_manager_never_the_agent(setup, monkeypatch):
    sb.VaultBackend._HELD.clear()
    ident = ed25519.Ed25519PrivateKey.generate()
    enc = x25519.X25519PrivateKey.generate()
    wallet = Account.create()
    bin_ = _fake_vault(setup["tmp"], {"op://Agents/be/identity": ident.private_bytes_raw(),
                                      "enc": enc.private_bytes_raw(), "op://Agents/be/evm": bytes(wallet.key)})
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    signer.save_backend("vault", "vault", {}, {"op_service_account_token": "fake-token", "bw_session": "fake-token"})
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in os.environ and "BW_SESSION" not in os.environ                    # the token is the signer's folder's
    signer.set_route(AGENT, "identity", "vault", "cmd:op read op://Agents/be/identity")
    signer.set_route(AGENT, "encryption", "vault", "cmd:bw get password enc")
    signer.set_route(AGENT, "evm", "vault", "cmd:op read op://Agents/be/evm")
    pub = ident.public_key().public_bytes_raw().hex()
    assert signer.unlock() == [] and signer.identity_public(AGENT) == pub
    _marker(setup["keys"], {"identity": pub, "encryption": enc.public_key().public_bytes_raw().hex()})
    p, sig = signing.signed(AGENT, "room-message", "room-1", "from the vault", "chat", "n" * 32)
    assert identity.verify(p, sig, pub)
    aad = crypto_box.message_aad("dm", "dm_1", "bob", "chat", "n1")
    env = crypto_box.seal("sealed hello", {AGENT: enc.public_key().public_bytes_raw().hex()}, aad)
    assert crypto_box.open_sealed(env, AGENT, identity.encryption_private_keys(AGENT), aad) == "sealed hello"
    assert agent_wallet.address(AGENT) == wallet.address
    assert not list(setup["keys"].glob("*.priv"))
    with pytest.raises(sb.Unsupported):
        signer.get_backend("vault").make(AGENT, "evm")


def test_a_locked_vault_is_said_at_start_without_a_value(setup, monkeypatch):
    sb.VaultBackend._HELD.clear()
    bin_ = _fake_vault(setup["tmp"], {"k": b"\1" * 32})
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    signer.save_backend("vault", "vault", {}, {"op_service_account_token": "wrong"})
    signer.set_route(AGENT, "evm", "vault", "cmd:op read k")
    problems = signer.unlock()
    assert problems and "not signed in" in problems[0] and "0101" not in problems[0]


# ─── settings ─────────────────────────────────────────────────────────────────

def test_back_end_settings_are_checked_and_credentials_stay_in_the_signers_folder(setup):
    with pytest.raises(ValueError, match="takes app_id"):
        signer.save_backend("p", "privy", {"app_secret": "x"}, {})           # a secret is not a setting
    with pytest.raises(ValueError, match="not 'local'"):
        signer.save_backend("local", "aws-kms", {}, {})
    signer.save_backend("p2", "privy", {"app_id": "a"}, {"app_secret": "hush"})
    secret = signer.signer_dir() / "backends" / "p2.secret"
    assert oct(secret.stat().st_mode & 0o777) == "0o600" and "hush" not in (signer.signer_dir() / "backends.json").read_text()
    assert "hush" not in json.dumps(signer.get_backend("p2").describe())
    signer.make_agent_keys(AGENT, {"evm": "kms1"})
    with pytest.raises(ValueError, match="still holds keys"):
        signer.remove_backend("kms1")


def test_routing_is_per_role_and_defaults_to_local(setup):
    made = signer.make_agent_keys(AGENT, {"evm": "kms1"})
    assert set(signer.routes(AGENT)) == {"evm"}
    assert signer.route(AGENT, "identity")[0].type == "local" and signer.route(AGENT, "evm")[0].type == "aws-kms"
    assert (signer.agent_dir(AGENT) / "keys" / "identity.ed25519").exists()
    assert signer.Wallet(AGENT).families() == ("evm", "svm") and made["evm"].startswith("0x")
    with pytest.raises(sb.BackendError, match="already holds"):
        signer.make_agent_keys(AGENT)                                       # never a second key in place
