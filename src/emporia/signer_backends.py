"""The signer's back ends: where its keys are kept and who does the key work
(signer phase 2, docs/proposals/signer-phase-2.md § 3.1).

The signer decides **whether** to sign: it builds the payload from a kind and
its arguments, applies the rule and the person's limits (`signing.check`, the
wallet's caps in `agent_wallet`), and only then hands the work to a back end.
A back end decides nothing; it does what it says it can, by **kind**:

| capability        | what                                                          |
|-------------------|---------------------------------------------------------------|
| `identity`        | Ed25519 over a payload the signer built (the agent's identity)  |
| `open`            | open one sealed message (X25519) — the local back end only     |
| `evm-typed-data`  | EIP-712 typed data: x402's EIP-3009, an escrow hold's ReceiveWithAuthorization |
| `evm-transaction` | an EIP-1559 transaction: a Tempo payment, an escrow reclaim     |
| `solana`          | Ed25519 over a Solana transaction message (x402 on Solana)     |
| `make-keys`       | new keys made inside it, which never exist anywhere else       |

A back end asked for something outside its capabilities is refused with a
reason (`Unsupported`), never quietly done another way.

The back ends:
* `local` — the signer's own 0600 files. The default for every role, and the
  only one that opens sealed messages: wallet providers sign, they don't decrypt.
* `vault` — a password manager (1Password, Bitwarden): the signer reads each key
  with a `cmd:` reference and holds it in memory; every capability but making keys.
* `aws-kms` — AWS KMS: wallet keys (secp256k1, `ECC_SECG_P256K1`), the identity
  key and Solana keys (Ed25519, `ECC_NIST_EDWARDS25519` with `ED25519_SHA_512`).
  KMS signs at most 4096 bytes of raw message with Ed25519, so a payload larger
  than that (a long sealed room message) is refused there.
* `privy` — a Privy server wallet: EVM typed data (`eth_signTypedData_v4`) and
  Solana transactions (`signTransaction`), each under the provider's policy.
* `cdp` — a Coinbase Developer Platform server wallet: EVM typed data
  (`/v2/evm/accounts/{address}/sign/typed-data`) under CDP's policy engine.
* `metamask` — a MetaMask Agent Wallet server wallet, through its `mm`
  command (`mm wallet sign-typed-data`): EVM typed data, in Guard Mode, under
  this back end's own limits (chains, token contracts, value) as well.

A hosted wallet's own policy is a second set of limits the agent cannot change.
Neither holds the identity key (no raw Ed25519 signing of Emporia's payloads)
or opens messages.

`aws-kms`, `privy`, `cdp` and `metamask` are tested against fakes, never against the live
services: `LIVE_TESTED` says so, and `emporia signer backend` prints it.

**Credentials** live in the signer's folder only (`backends/<name>.secret`,
0600) or in the signer user's own environment (AWS's usual chain). Nothing
here is read from, or written to, an agent's profile.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, ClassVar

ROLES = ("identity", "encryption", "evm", "svm")
CAPABILITIES = ("identity", "open", "evm-typed-data", "evm-transaction", "solana", "make-keys")
# What a role needs from the back end that holds it.
ROLE_NEEDS = {"identity": "identity", "encryption": "open", "evm": "evm-typed-data", "svm": "solana"}
TYPES = ("local", "vault", "aws-kms", "privy", "cdp", "metamask")
# In the signer, vault is tested with fake `op` and `bw` commands on PATH, metamask with a fake `mm`;
# the others against fakes of their APIs. None has been tried against the real service from the signer yet.
LIVE_TESTED = {"local": True, "vault": False, "aws-kms": False, "privy": False, "cdp": False, "metamask": False}
NOT_LIVE_NOTE = "not yet tested against the live service"


class BackendError(RuntimeError):
    """The back end could not do it (unreachable, misconfigured, the provider said no)."""


class Unsupported(BackendError):
    """Asked for a capability this back end does not have."""


@dataclass
class Backend:
    """The interface. `ref` names the key inside the back end: nothing for the
    local files, a KMS key id or ARN, a provider's wallet id."""
    name: str
    settings: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, Any] = field(default_factory=dict)
    type: ClassVar[str] = ""
    capabilities: ClassVar[frozenset[str]] = frozenset()

    @property
    def live_tested(self) -> bool:
        return LIVE_TESTED.get(self.type, False)

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "capabilities": sorted(self.capabilities),
                "live_tested": self.live_tested,
                "settings": {k: v for k, v in self.settings.items()}, "secrets": sorted(self.secrets)}

    def require(self, capability: str, what: str = "") -> None:
        if capability not in self.capabilities:
            raise Unsupported(f"the {self.name} back end ({self.type}) cannot {what or capability}: it does "
                              f"{', '.join(sorted(self.capabilities)) or 'nothing'}")

    def can_hold(self, role: str) -> bool:
        return ROLE_NEEDS[role] in self.capabilities

    # ── the key work, by kind ────────────────────────────────────────────────
    def public(self, agent_id: str, role: str, ref: str = "") -> str:
        """The public half: hex for identity and encryption, the address for evm and svm."""
        raise Unsupported(f"the {self.name} back end holds no {role} key")

    def sign_ed25519(self, agent_id: str, role: str, data: bytes, ref: str = "") -> bytes:
        self.require("identity" if role == "identity" else "solana", f"sign as {role}")
        raise NotImplementedError

    def sign_typed_data(self, agent_id: str, full_message: dict, ref: str = "") -> bytes:
        self.require("evm-typed-data", "sign EIP-712 typed data")
        raise NotImplementedError

    def sign_transaction(self, agent_id: str, tx: dict, ref: str = "") -> bytes:
        self.require("evm-transaction", "sign an EVM transaction")
        raise NotImplementedError

    def open_sealed(self, agent_id: str, envelope: dict, label: str, aad: bytes, ref: str = "") -> str:
        self.require("open", "open sealed messages")
        raise NotImplementedError

    def make(self, agent_id: str, role: str) -> str:
        """Make a new key for `role` inside the back end; return its ref."""
        self.require("make-keys", "make keys")
        raise NotImplementedError


# ─── shared EVM helpers ───────────────────────────────────────────────────────

def typed_data_digest(full_message: dict) -> bytes:
    """The EIP-712 hash a wallet signs: keccak(0x19 ‖ 0x01 ‖ domainSeparator ‖ structHash)."""
    from eth_account.messages import encode_typed_data
    from eth_utils import keccak
    m = encode_typed_data(full_message=full_message)
    return keccak(b"\x19" + m.version + m.header + m.body)


def recover_typed_data(full_message: dict, signature: bytes) -> str:
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    return Account.recover_message(encode_typed_data(full_message=full_message), signature=signature)


def _address_of_secp256k1(pub) -> str:
    from cryptography.hazmat.primitives import serialization
    from eth_utils import keccak, to_checksum_address
    raw = pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return to_checksum_address(keccak(raw[1:])[-20:])


_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def rsv_from_der(der: bytes, digest: bytes, address: str) -> tuple[int, int, int]:
    """(recovery id, r, s) for a DER ECDSA signature over `digest` by `address`'s
    key — low-s, as Ethereum requires; the recovery id found by trying both."""
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from eth_keys import keys
    r, s = decode_dss_signature(der)
    if s > _SECP256K1_N // 2:
        s = _SECP256K1_N - s
    for rec in (0, 1):
        try:
            if keys.Signature(vrs=(rec, r, s)).recover_public_key_from_msg_hash(
                    digest).to_checksum_address() == address:
                return rec, r, s
        except Exception:  # noqa: BLE001 — not this recovery id
            continue
    raise BackendError("the back end's signature is not from the key it named")


def transaction_digest(tx: dict) -> bytes:
    from eth_account.typed_transactions import TypedTransaction
    if int(tx.get("type", 0)) != 2:
        raise Unsupported("only EIP-1559 (type 2) transactions are signed through a back end")
    return TypedTransaction.from_dict(tx).hash()


def encode_signed_transaction(tx: dict, rec: int, r: int, s: int) -> bytes:
    from eth_account.typed_transactions import TypedTransaction
    return TypedTransaction.from_dict({**tx, "v": rec, "r": r, "s": s}).encode()


def _verify_ed25519(public_hex: str, signature: bytes, data: bytes) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import ed25519
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(signature, data)
    except (InvalidSignature, ValueError) as e:
        raise BackendError("the back end's signature does not verify with the key it named") from e


def _b58(raw: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n, out = int.from_bytes(raw, "big"), ""
    while n:
        n, rem = divmod(n, 58)
        out = alphabet[rem] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


# ─── local: the signer's own files ────────────────────────────────────────────

class LocalBackend(Backend):
    """Keys in the signer's folder, readable only by its user (`agents/<id>/keys`)."""
    type = "local"
    capabilities = frozenset(CAPABILITIES)

    def _keys(self, agent_id: str):
        from emporia import signer
        return signer.agent_dir(agent_id) / "keys"

    def _wallet_scope(self, agent_id: str):
        from emporia import agent_wallet
        return agent_wallet.scope(agent_wallet.Scope(env={}, key_dir=self._keys(agent_id)))

    def _path(self, agent_id: str, role: str):
        from emporia import agent_wallet
        if role == "identity":
            return self._keys(agent_id) / "identity.ed25519"
        if role == "encryption":
            return self._keys(agent_id) / "encryption.x25519"
        with self._wallet_scope(agent_id):
            return agent_wallet.wallet_path(agent_id) if role == "evm" else agent_wallet.solana_wallet_path(agent_id)

    def holds(self, agent_id: str, role: str) -> bool:
        return self._path(agent_id, role).is_file()

    def _raw(self, agent_id: str, role: str, ref: str = "") -> bytes:
        path = self._path(agent_id, role)
        try:
            raw = path.read_bytes()
        except OSError:
            raise BackendError(f"the signer holds no {role} key for {agent_id}") from None
        if len(raw) != 32:
            raise BackendError(f"{path} is {len(raw)} bytes, expected 32; restore it from a backup")
        return raw

    def public(self, agent_id: str, role: str, ref: str = "") -> str:
        from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
        raw = self._raw(agent_id, role, ref)
        if role == "identity":
            return ed25519.Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw().hex()
        if role == "encryption":
            return x25519.X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw().hex()
        if role == "evm":
            from eth_account import Account
            return Account.from_key(raw).address
        return _b58(ed25519.Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw())

    def sign_ed25519(self, agent_id: str, role: str, data: bytes, ref: str = "") -> bytes:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        if role not in ("identity", "svm"):
            raise Unsupported(f"no Ed25519 key for {role}")
        return ed25519.Ed25519PrivateKey.from_private_bytes(self._raw(agent_id, role, ref)).sign(data)

    def _account(self, agent_id: str, ref: str = ""):
        from eth_account import Account
        return Account.from_key(self._raw(agent_id, "evm", ref))

    def sign_typed_data(self, agent_id: str, full_message: dict, ref: str = "") -> bytes:
        return bytes(self._account(agent_id, ref).sign_typed_data(full_message=full_message).signature)

    def sign_transaction(self, agent_id: str, tx: dict, ref: str = "") -> bytes:
        return bytes(self._account(agent_id, ref).sign_transaction(tx).raw_transaction)

    def encryption_keys(self, agent_id: str) -> list:
        """The current X25519 key and the ones it replaced, newest first."""
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

        from emporia import identity
        path = self._path(agent_id, "encryption")
        if not path.exists():
            return []
        keys = [X25519PrivateKey.from_private_bytes(path.read_bytes())]
        retired = path.with_name("encryption.retired")
        if retired.exists():
            keys += [X25519PrivateKey.from_private_bytes(bytes.fromhex(h))
                     for h in identity._parse_retired(retired.read_text())]
        return keys

    def open_sealed(self, agent_id: str, envelope: dict, label: str, aad: bytes, ref: str = "") -> str:
        from emporia import crypto_box
        keys = self.encryption_keys(agent_id)
        if not keys:
            raise BackendError(f"the signer holds no encryption key for {agent_id}")
        return crypto_box.open_sealed(envelope, label, keys, aad)

    def make(self, agent_id: str, role: str) -> str:
        import secrets as _secrets

        from emporia import identity
        path = self._path(agent_id, role)
        if path.exists():
            raise BackendError(f"the signer already holds a {role} key for {agent_id}; a key is never replaced "
                               "implicitly")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if role == "evm":
            from eth_account import Account
            raw = bytes(Account.create().key)
        else:
            raw = _secrets.token_bytes(32)          # an Ed25519 seed or an X25519 scalar: 32 random bytes
        identity._write_secret_atomically(path, raw)
        return ""

    def replace_encryption_key(self, agent_id: str) -> tuple[str, str]:
        """A new X25519 key made here; the old one kept, retired, to open what was
        sealed to it. (old public hex, new public hex)."""
        import secrets as _secrets

        from cryptography.hazmat.primitives.asymmetric import x25519

        from emporia import identity
        path = self._path(agent_id, "encryption")
        old = self._raw(agent_id, "encryption")
        retired = path.with_name("encryption.retired")
        kept = [old.hex()] + (identity._parse_retired(retired.read_text()) if retired.exists() else [])
        identity._write_secret_atomically(retired, ("\n".join(dict.fromkeys(kept)) + "\n").encode())
        new = _secrets.token_bytes(32)
        identity._write_secret_atomically(path, new)
        pub = lambda raw: x25519.X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw().hex()  # noqa: E731
        return pub(old), pub(new)


# ─── a password manager: keys read into the signer ───────────────────────────

class VaultBackend(LocalBackend):
    """Keys kept in a password manager — 1Password (`cmd:op read op://…`, with a
    service account token) or Bitwarden (`cmd:bw get password ITEM`, with a
    session) — read **by the signer**, at its start or first use, and held in its
    memory while it runs. The vault token is the signer's (its own environment,
    or `backends/<name>.secret`), never an agent's; the agent's profile names no
    vault at all.

    The trade-off: a password manager stores keys, it does not sign, so the key is
    in the signer's memory while the signer runs. Safer than a file (nothing on
    disk to copy, and the vault's own access log), less than KMS, which never
    releases a key. Everything a local key does, except making keys: a new key is
    put into the vault by its person."""
    type = "vault"
    capabilities = frozenset({"identity", "open", "evm-typed-data", "evm-transaction", "solana"})
    # (back end, ref) → the key, for the life of the signer process. Never written anywhere.
    _HELD: ClassVar[dict[tuple[str, str], bytes]] = {}

    # The vault CLIs' own variables, from the back end's secrets when its person kept them there.
    TOKEN_VARS = {"op_service_account_token": "OP_SERVICE_ACCOUNT_TOKEN", "bw_session": "BW_SESSION"}

    def holds(self, agent_id: str, role: str) -> bool:
        return False                                  # it holds what a route names; nothing by file

    def _raw(self, agent_id: str, role: str, ref: str = "") -> bytes:
        from emporia import key_source
        if not ref.startswith("cmd:"):
            raise BackendError(f"the {self.name} back end reads a key from cmd:op read op://… or "
                               f"cmd:bw get password ITEM, not {ref.split(':')[0] or 'nothing'}")
        key = (self.name, ref)
        if key not in self._HELD:
            env = {var: self.secrets[k] for k, var in self.TOKEN_VARS.items() if self.secrets.get(k)}
            try:
                self._HELD[key] = key_source.key32(ref, env or None)
            except key_source.KeySourceError as e:
                raise BackendError(f"the {self.name} back end could not read the {role} key: {e}") from None
        return self._HELD[key]

    def encryption_keys(self, agent_id: str, ref: str = "") -> list:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        return [X25519PrivateKey.from_private_bytes(self._raw(agent_id, "encryption", ref))]

    def open_sealed(self, agent_id: str, envelope: dict, label: str, aad: bytes, ref: str = "") -> str:
        from emporia import crypto_box
        return crypto_box.open_sealed(envelope, label, self.encryption_keys(agent_id, ref), aad)

    def make(self, agent_id: str, role: str) -> str:
        raise Unsupported(f"the {self.name} back end ({self.type}) reads keys its person keeps in the vault; "
                          "it makes none — put a new key there, or make it with the local back end")

    def replace_encryption_key(self, agent_id: str) -> tuple[str, str]:
        raise Unsupported("an encryption key kept in a password manager is replaced there by its person")


# ─── AWS KMS ──────────────────────────────────────────────────────────────────

# KMS signs at most this many bytes of raw message (ED25519_SHA_512, MessageType RAW).
KMS_MAX_RAW_BYTES = 4096


class KmsBackend(Backend):
    """AWS KMS. Settings: `region`, optionally `profile` (an AWS profile of the
    signer's own user). Credentials come from AWS's usual chain in the signer's
    environment (its user's ~/.aws, an instance role), never from an agent.

    * wallet keys: `ECC_SECG_P256K1`, `ECDSA_SHA_256` over the EIP-712 or
      transaction digest (`MessageType=DIGEST`); the DER signature is made low-s
      and given its recovery id here.
    * identity and Solana keys: `ECC_NIST_EDWARDS25519`, `ED25519_SHA_512`
      (`MessageType=RAW`, at most 4096 bytes — standard Ed25519, so relays and
      peers verify it as they do any agent's).

    `client` is for tests (a fake with KMS's methods); otherwise boto3, from the
    optional `kms` extra."""
    type = "aws-kms"
    capabilities = frozenset({"identity", "evm-typed-data", "evm-transaction", "solana", "make-keys"})

    def __init__(self, name: str, settings: dict | None = None, secrets: dict | None = None, client: Any = None):
        super().__init__(name, dict(settings or {}), dict(secrets or {}))
        self._client = client
        self._pubs: dict[str, Any] = {}

    def client(self):
        if self._client is None:
            try:
                import boto3
            except ImportError:
                raise BackendError("the aws-kms back end needs boto3: install Emporia with its kms extra "
                                   "(pip install 'emporia[kms]')") from None
            session = boto3.session.Session(profile_name=self.settings.get("profile") or None)
            self._client = session.client("kms", region_name=self.settings.get("region") or None)
        return self._client

    def _call(self, op: str, **kw):
        try:
            return getattr(self.client(), op)(**kw)
        except BackendError:
            raise
        except Exception as e:  # noqa: BLE001 — botocore's errors, said without a traceback
            raise BackendError(f"AWS KMS {op}: {type(e).__name__}: {str(e)[:200]}") from None

    def _public_key(self, ref: str):
        if not ref:
            raise BackendError(f"the {self.name} back end needs a KMS key id for this key")
        if ref not in self._pubs:
            from cryptography.hazmat.primitives.serialization import load_der_public_key
            self._pubs[ref] = load_der_public_key(self._call("get_public_key", KeyId=ref)["PublicKey"])
        return self._pubs[ref]

    def public(self, agent_id: str, role: str, ref: str = "") -> str:
        from cryptography.hazmat.primitives.asymmetric import ec, ed25519
        self.require(ROLE_NEEDS[role] if role in ROLE_NEEDS else role, f"hold a {role} key")
        pub = self._public_key(ref)
        if role == "evm":
            if not isinstance(pub, ec.EllipticCurvePublicKey) or pub.curve.name != "secp256k1":
                raise BackendError(f"KMS key {ref} is not ECC_SECG_P256K1")
            return _address_of_secp256k1(pub)
        if not isinstance(pub, ed25519.Ed25519PublicKey):
            raise BackendError(f"KMS key {ref} is not ECC_NIST_EDWARDS25519")
        raw = pub.public_bytes_raw()
        return raw.hex() if role == "identity" else _b58(raw)

    def sign_ed25519(self, agent_id: str, role: str, data: bytes, ref: str = "") -> bytes:
        self.require("identity" if role == "identity" else "solana", f"sign as {role}")
        if len(data) > KMS_MAX_RAW_BYTES:
            raise Unsupported(f"AWS KMS signs at most {KMS_MAX_RAW_BYTES} bytes with Ed25519; this is "
                              f"{len(data)} (a shorter message, or the local back end for the identity key)")
        sig = self._call("sign", KeyId=ref, Message=data, MessageType="RAW",
                         SigningAlgorithm="ED25519_SHA_512")["Signature"]
        from cryptography.hazmat.primitives.asymmetric import ed25519
        pub = self._public_key(ref)
        if not isinstance(pub, ed25519.Ed25519PublicKey):
            raise BackendError(f"KMS key {ref} is not ECC_NIST_EDWARDS25519")
        _verify_ed25519(pub.public_bytes_raw().hex(), sig, data)
        return bytes(sig)

    def _sign_digest(self, ref: str, digest: bytes) -> tuple[int, int, int]:
        der = self._call("sign", KeyId=ref, Message=digest, MessageType="DIGEST",
                         SigningAlgorithm="ECDSA_SHA_256")["Signature"]
        return rsv_from_der(bytes(der), digest, self.public("", "evm", ref))

    def sign_typed_data(self, agent_id: str, full_message: dict, ref: str = "") -> bytes:
        self.require("evm-typed-data", "sign EIP-712 typed data")
        digest = typed_data_digest(full_message)
        rec, r, s = self._sign_digest(ref, digest)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([27 + rec])

    def sign_transaction(self, agent_id: str, tx: dict, ref: str = "") -> bytes:
        self.require("evm-transaction", "sign an EVM transaction")
        digest = transaction_digest(tx)
        rec, r, s = self._sign_digest(ref, digest)
        return encode_signed_transaction(tx, rec, r, s)

    def make(self, agent_id: str, role: str) -> str:
        self.require("make-keys", "make keys")
        if role == "encryption":
            raise Unsupported("AWS KMS has no X25519 keys: the encryption key stays with the local back end")
        spec = "ECC_SECG_P256K1" if role == "evm" else "ECC_NIST_EDWARDS25519"
        meta = self._call("create_key", KeySpec=spec, KeyUsage="SIGN_VERIFY",
                          Description=f"Emporia {agent_id} {role} key",
                          Tags=[{"TagKey": "emporia-agent", "TagValue": agent_id},
                                {"TagKey": "emporia-role", "TagValue": role}])["KeyMetadata"]
        return meta.get("Arn") or meta["KeyId"]


# ─── hosted wallets: Privy and Coinbase Developer Platform ────────────────────


def _json_ready(value: Any) -> Any:
    """Typed data as JSON: bytes as 0x hex, integers past JavaScript's safe range as strings."""
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) < 2 ** 53 else str(value)
    if isinstance(value, dict):
        return {k: _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    """RFC 8785 for what a Privy request body holds (objects, arrays, strings,
    integers, booleans): sorted keys, no spaces. CDP's request hash sorts the same way."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class _Hosted(Backend):
    """What both hosted wallets share: HTTP to the provider (a test's
    `httpx.MockTransport` in place of the network), and every signature checked
    here against the wallet's own address before the signer uses it."""
    API = ""

    def __init__(self, name: str, settings: dict | None = None, secrets: dict | None = None, transport: Any = None):
        super().__init__(name, dict(settings or {}), dict(secrets or {}))
        self._transport = transport
        self._addresses: dict[str, str] = {}

    def _base(self) -> str:
        return (self.settings.get("base_url") or self.API).rstrip("/")

    def _headers(self, method: str, path: str, body: dict | None) -> dict[str, str]:
        raise NotImplementedError

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        import httpx
        try:
            with httpx.Client(base_url=self._base(), timeout=30, transport=self._transport) as h:
                r = h.request(method, path, json=body, headers=self._headers(method, path, body))
        except httpx.HTTPError as e:
            raise BackendError(f"{self.type} is not reachable: {e}") from None
        if r.status_code not in (200, 201):
            try:
                j = r.json()
                detail = j.get("errorMessage") or j.get("error") or j.get("message") or r.text[:200]
            except ValueError:
                detail = r.text[:200]
            raise BackendError(f"{self.type} refused ({r.status_code}): {detail}")
        return r.json()

    def _check_evm(self, agent_id: str, full_message: dict, sig: bytes, ref: str) -> bytes:
        if len(sig) != 65 or recover_typed_data(full_message, sig) != self.public(agent_id, "evm", ref):
            raise BackendError(f"{self.type}'s signature is not from the wallet it named")
        return sig


class PrivyBackend(_Hosted):
    """A Privy server wallet (https://docs.privy.io). Settings: `app_id`, and
    optionally `policy_id` (attached to each wallet made here), `owner_id` and
    `base_url`. Secrets, in the signer's folder only: `app_secret`, and
    `authorization_key` (`wallet-auth:…`) when its wallets have an owner — each
    request then carries a P-256 authorization signature.

    * EVM: `POST /v1/wallets/{id}/rpc` with `eth_signTypedData_v4` — x402's
      EIP-3009 and an escrow hold's ReceiveWithAuthorization.
    * Solana: the same call with `signTransaction` (a base64 transaction): the
      x402 Solana payment, which the facilitator completes as fee payer.

    Privy's policy engine (rules per method, conditions on the typed data's
    domain and message, or on a Solana transfer's program, mint, destination and
    amount; anything unmatched is denied) refuses what its person did not allow,
    a second set of limits the agent cannot change."""
    type = "privy"
    API = "https://api.privy.io"
    capabilities = frozenset({"evm-typed-data", "solana", "make-keys"})

    def _headers(self, method: str, path: str, body: dict | None) -> dict[str, str]:
        app_id, app_secret = self.settings.get("app_id", ""), self.secrets.get("app_secret", "")
        if not app_id or not app_secret:
            raise BackendError(f"the {self.name} back end needs its app id and app secret "
                               f"(emporia signer backend {self.name} add privy app_id=…)")
        headers = {"authorization": "Basic " + base64.b64encode(f"{app_id}:{app_secret}".encode()).decode(),
                   "privy-app-id": app_id, "content-type": "application/json"}
        if (key := self.secrets.get("authorization_key", "")) and method != "GET":
            headers["privy-authorization-signature"] = self.authorization_signature(
                key, method, self._base() + path, body or {})
        return headers

    def authorization_signature(self, key: str, method: str, url: str, body: dict) -> str:
        """Privy's request signature: ECDSA P-256 (SHA-256, DER, base64) over the
        canonical JSON of {version, method, url, body, headers: {privy-app-id}}."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        priv = _p256_key(key.removeprefix("wallet-auth:"), f"the {self.name} back end's authorization key")
        payload = {"version": 1, "method": method, "url": url, "body": body,
                   "headers": {"privy-app-id": self.settings.get("app_id", "")}}
        return base64.b64encode(priv.sign(canonical_json(payload).encode(), ec.ECDSA(hashes.SHA256()))).decode()

    def public(self, agent_id: str, role: str, ref: str = "") -> str:
        if role not in ("evm", "svm"):
            raise Unsupported(f"the {self.name} back end (privy) holds wallet keys (evm, svm), not {role}")
        if not ref:
            raise BackendError(f"the {self.name} back end needs a Privy wallet id for this key")
        if ref not in self._addresses:
            address = str(self._request("GET", f"/v1/wallets/{ref}")["address"])
            if role == "evm":
                from eth_utils import to_checksum_address
                address = to_checksum_address(address)
            self._addresses[ref] = address
        return self._addresses[ref]

    def sign_typed_data(self, agent_id: str, full_message: dict, ref: str = "") -> bytes:
        self.require("evm-typed-data", "sign EIP-712 typed data")
        body = {"method": "eth_signTypedData_v4", "params": {"typed_data": _json_ready({
            "domain": full_message["domain"], "types": full_message["types"],
            "message": full_message["message"], "primary_type": full_message["primaryType"]})}}
        if chain := full_message.get("domain", {}).get("chainId"):
            body["caip2"] = f"eip155:{int(chain)}"
        out = self._request("POST", f"/v1/wallets/{ref}/rpc", body)
        try:
            sig = bytes.fromhex(str(out["data"]["signature"]).removeprefix("0x"))
        except (KeyError, TypeError, ValueError):
            raise BackendError("Privy's answer carried no signature") from None
        return self._check_evm(agent_id, full_message, sig, ref)

    def sign_ed25519(self, agent_id: str, role: str, data: bytes, ref: str = "") -> bytes:
        """A Solana payment: the message goes to Privy as an unsigned transaction
        (`signTransaction`, which its policy sees whole), and the payer's
        signature comes back out of the signed one, checked here."""
        if role != "svm":
            raise Unsupported(f"the {self.name} back end (privy) signs Solana transactions, not raw {role} bytes")
        self.require("solana", "sign a Solana transaction")
        from solders.message import from_bytes_versioned
        from solders.pubkey import Pubkey
        from solders.signature import Signature
        from solders.transaction import VersionedTransaction
        message = from_bytes_versioned(bytes(data))
        signers = list(message.account_keys[:message.header.num_required_signatures])
        me = Pubkey.from_string(self.public(agent_id, "svm", ref))
        if me not in signers:
            raise BackendError("the Solana transaction does not need this wallet's signature")
        tx = VersionedTransaction.populate(message, [Signature.default()] * len(signers))
        out = self._request("POST", f"/v1/wallets/{ref}/rpc", {
            "method": "signTransaction",
            "params": {"transaction": base64.b64encode(bytes(tx)).decode(), "encoding": "base64"}})
        try:
            signed = VersionedTransaction.from_bytes(base64.b64decode(out["data"]["signed_transaction"]))
        except (KeyError, TypeError, ValueError):
            raise BackendError("Privy's answer carried no signed transaction") from None
        if bytes(signed.message) != bytes(message):
            raise BackendError("Privy signed a different transaction than the one it was given")
        sig = bytes(signed.signatures[signers.index(me)])
        _verify_ed25519(bytes(me).hex(), sig, bytes(data))
        return sig

    def make(self, agent_id: str, role: str) -> str:
        self.require("make-keys", "make keys")
        if role not in ("evm", "svm"):
            raise Unsupported(f"the {self.name} back end (privy) makes wallets (evm, svm), not {role}")
        body: dict[str, Any] = {"chain_type": "ethereum" if role == "evm" else "solana"}
        if self.settings.get("policy_id"):
            body["policy_ids"] = [self.settings["policy_id"]]
        if self.settings.get("owner_id"):
            body["owner_id"] = self.settings["owner_id"]
        out = self._request("POST", "/v1/wallets", body)
        self._addresses.pop(out["id"], None)
        return out["id"]


class CdpBackend(_Hosted):
    """A Coinbase Developer Platform server wallet (v2 API,
    https://docs.cdp.coinbase.com). Settings: `api_key_id`, optionally
    `policy_id` (set as each new account's policy) and `base_url`. Secrets, in the
    signer's folder only: `api_key_secret` (an Ed25519 key, base64, or an EC key
    in PEM) and `wallet_secret` (base64 PKCS#8 P-256) — every request carries a
    bearer JWT from the first, every signing request a wallet JWT
    (`X-Wallet-Auth`, over a hash of its body) from the second.

    EVM typed data only: `POST /v2/evm/accounts/{address}/sign/typed-data`, the
    account named by its address. CDP's policy engine (`signEvmTypedData` rules:
    which verifying contracts, conditions on the message's fields) is the second
    layer of limits."""
    type = "cdp"
    API = "https://api.cdp.coinbase.com/platform"
    capabilities = frozenset({"evm-typed-data", "make-keys"})

    def _uri(self, method: str, path: str) -> str:
        from urllib.parse import urlparse
        u = urlparse(self._base() + path)
        return f"{method.upper()} {u.netloc}{u.path}"

    def _headers(self, method: str, path: str, body: dict | None) -> dict[str, str]:
        import secrets as _secrets
        import time
        key_id, key_secret = self.settings.get("api_key_id", ""), self.secrets.get("api_key_secret", "")
        if not key_id or not key_secret:
            raise BackendError(f"the {self.name} back end needs its API key id and secret "
                               f"(emporia signer backend {self.name} add cdp api_key_id=…)")
        now = int(time.time())
        bearer = _jwt({"kid": key_id, "nonce": _secrets.token_hex(16), "typ": "JWT"},
                      {"sub": key_id, "iss": "cdp", "aud": ["cdp_service"], "nbf": now, "exp": now + 120,
                       "uri": self._uri(method, path)}, key_secret, "the API key secret")
        headers = {"authorization": f"Bearer {bearer}", "content-type": "application/json"}
        if method != "GET":
            wallet_secret = self.secrets.get("wallet_secret", "")
            if not wallet_secret:
                raise BackendError(f"the {self.name} back end needs its wallet secret to sign")
            claims: dict[str, Any] = {"iat": now, "nbf": now, "jti": _secrets.token_hex(16),
                                      "uris": [self._uri(method, path)]}
            if body:
                import hashlib
                claims["reqHash"] = hashlib.sha256(canonical_json(body).encode()).hexdigest()
            headers["x-wallet-auth"] = _jwt({"typ": "JWT"}, claims, wallet_secret, "the wallet secret", es256=True)
        return headers

    def public(self, agent_id: str, role: str, ref: str = "") -> str:
        if role != "evm":
            raise Unsupported(f"the {self.name} back end (cdp) holds EVM wallet keys only, not {role}")
        if not ref:
            raise BackendError(f"the {self.name} back end needs the CDP account's address for this key")
        from eth_utils import is_address, to_checksum_address
        if not is_address(ref):
            raise BackendError(f"{ref!r} is not an EVM address (a CDP account is named by its address)")
        return to_checksum_address(ref)

    def sign_typed_data(self, agent_id: str, full_message: dict, ref: str = "") -> bytes:
        self.require("evm-typed-data", "sign EIP-712 typed data")
        address = self.public(agent_id, "evm", ref)
        out = self._request("POST", f"/v2/evm/accounts/{address}/sign/typed-data", _json_ready({
            "domain": full_message["domain"], "types": full_message["types"],
            "primaryType": full_message["primaryType"], "message": full_message["message"]}))
        try:
            sig = bytes.fromhex(str(out["signature"]).removeprefix("0x"))
        except (KeyError, TypeError, ValueError):
            raise BackendError("CDP's answer carried no signature") from None
        return self._check_evm(agent_id, full_message, sig, ref)

    def make(self, agent_id: str, role: str) -> str:
        self.require("make-keys", "make keys")
        if role != "evm":
            raise Unsupported(f"the {self.name} back end (cdp) makes EVM accounts only, not {role}")
        import re
        body: dict[str, Any] = {"name": re.sub(r"[^A-Za-z0-9-]", "-", f"emporia-{agent_id}")[:36]}
        if self.settings.get("policy_id"):
            body["accountPolicy"] = self.settings["policy_id"]
        from eth_utils import to_checksum_address
        return to_checksum_address(self._request("POST", "/v2/evm/accounts", body)["address"])


# ─── MetaMask Agent Wallet, through its `mm` command ─────────────────────────

# What Emporia signs as typed data: x402's EIP-3009 and an escrow hold's.
MM_PRIMARY_TYPES = ("TransferWithAuthorization", "ReceiveWithAuthorization")
# The `mm` error codes that mean its session is gone (docs: Error codes, AuthError / WalletError).
MM_SIGNED_OUT = frozenset({"AUTH_FAILED", "AUTH_ERROR", "TOKEN_INVALID", "TOKEN_REFRESH_FAILED", "NO_AUTH_TOKEN"})
# The codes that are MetaMask's own policy saying no, or its person not saying yes in time.
MM_REFUSALS = {"TX_DENIED": "its person rejected it in the 2FA approval",
               "TX_EXPIRED": "its 2FA approval expired",
               "JOB_TIMEOUT": "it waited for its person's 2FA approval and none came"}


class MetaMaskBackend(Backend):
    """A MetaMask Agent Wallet server wallet (https://docs.metamask.io/agent-wallet,
    CLI 7.2.0, checked 2026-11-06), through its `mm` command — MetaMask documents
    no HTTP API, so the signer runs `mm` as its own user, never the agent's.

    * **Signs:** `mm wallet sign-typed-data --chain-id N --payload JSON --wait
      --json` → `data.signature`. The command signs with the account's *active*
      wallet, so the signer selects the agent's (`mm wallet select ADDRESS`)
      first, one request at a time; the signature is then checked against that
      address, whatever `mm` was doing.
    * **Makes:** `mm wallet create --name … --trading-mode guard --json` (keys in
      MetaMask's TEE, never here). A wallet is named by its address.
    * **Credentials:** the `mm` session of the signer's user (`mm login` and
      `mm init --wallet server-wallet`, run by its person; `~/.metamask`). The
      optional `cli_token` secret (`cliToken:cliRefreshToken`, from the
      dashboard) signs `mm` in again when its session is gone, given in the
      environment (`MM_CLI_TOKEN`), never on the command line.

    Two layers of limits under the signer's own:
    * **This back end's:** only typed data Emporia signs (`MM_PRIMARY_TYPES`), on
      the chains in `chain_ids` (Base Sepolia by default), for the token contracts
      in `verifying_contracts` (USDC on those chains by default; `any` for none),
      for at most `max_value` base units. MetaMask's own outflow limit does not
      count signatures (its docs, "Outflow policy: Limitations"), so this is
      what bounds an authorization on the provider side.
    * **MetaMask's:** the wallet must be in Guard Mode (allowlists, threat
      scanning; checked before each first signature); a request outside its
      policy waits for its person's 2FA approval, at most `wait_seconds` (60),
      and comes back refused if none came.

    Not used: its bring-your-own-wallet mode (the phrase would be handed to a
    second program, and its policy 2FA is "coming soon"), raw transactions,
    message signing, transfers, swaps, everything else `mm` does."""
    type = "metamask"
    capabilities = frozenset({"evm-typed-data", "make-keys"})
    # One `mm` at a time per signer: select, then sign, with nothing between.
    _LOCK: ClassVar[Any] = None

    def __init__(self, name: str, settings: dict | None = None, secrets: dict | None = None, runner: Any = None):
        super().__init__(name, dict(settings or {}), dict(secrets or {}))
        self._runner = runner                       # tests: a fake in place of subprocess.run
        self._modes: dict[str, float] = {}          # address → when Guard Mode was last seen
        if MetaMaskBackend._LOCK is None:
            import threading
            MetaMaskBackend._LOCK = threading.Lock()

    # ── its settings, read strictly ──────────────────────────────────────────
    def _chain_ids(self) -> set[int]:
        raw = self.settings.get("chain_ids") or "84532"
        try:
            return {int(x) for x in str(raw).replace(" ", "").split(",") if x}
        except ValueError:
            raise BackendError(f"the {self.name} back end's chain_ids are chain numbers, not {raw!r}") from None

    def _contracts(self, chains: set[int]) -> set[str] | None:
        raw = str(self.settings.get("verifying_contracts") or "").strip()
        if raw.lower() == "any":
            return None
        if raw:
            return {x.strip().lower() for x in raw.split(",") if x.strip()}
        from emporia import networks
        return {n.usdc.lower() for c, n in networks.NETWORKS.items()
                if n.usdc and c.startswith("eip155:") and int(c.split(":")[1]) in chains}

    def _wait(self) -> int:
        try:
            return max(1, min(600, int(self.settings.get("wait_seconds") or 60)))
        except ValueError:
            raise BackendError(f"the {self.name} back end's wait_seconds is a number of seconds") from None

    # ── `mm` ──────────────────────────────────────────────────────────────────
    def _redact(self, text: str) -> str:
        for value in self.secrets.values():
            for part in {str(value), *str(value).split(":")}:
                if len(part) >= 8:
                    text = text.replace(part, "[redacted]")
        return text

    def _mm(self, *args: str, env: dict | None = None, timeout: int = 60) -> tuple[dict, list[dict]]:
        """Run `mm ARGS --json`; (its result, its notices). Machine output is a JSON
        object per line, the result last; anything else on stdout is ignored."""
        import os
        import subprocess
        run = self._runner or subprocess.run
        # Its own environment, never a phrase or token it did not ask for.
        base = {k: v for k, v in os.environ.items() if k not in ("MM_MNEMONIC", "MM_PASSWORD", "MM_CLI_TOKEN")}
        try:
            out = run([self.settings.get("mm") or "mm", *args, "--json"], capture_output=True, text=True,
                      timeout=timeout, env={**base, **(env or {})})
        except FileNotFoundError:
            raise BackendError(f"the {self.name} back end needs MetaMask's mm command where the signer runs "
                               "(npm install -g @metamask/agent-wallet; Node.js 22.18 or later)") from None
        except subprocess.TimeoutExpired:
            raise BackendError(f"MetaMask's mm did not answer in {timeout} s") from None
        objects = []
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    objects.append(json.loads(line))
                except ValueError:
                    continue
        if not objects and (text := out.stdout or "").find("{") != -1:
            try:
                objects.append(json.loads(text[text.find("{"): text.rfind("}") + 1]))
            except ValueError:
                pass
        notices = [o["_notice"] for o in objects if isinstance(o.get("_notice"), dict)]
        results = [o for o in objects if "_notice" not in o]
        if not results:
            raise BackendError(f"MetaMask's mm gave no answer (exit {out.returncode}): "
                               + self._redact((out.stderr or "").strip()[:200]))
        return results[-1], notices

    @staticmethod
    def _error(res: dict) -> tuple[str, str]:
        err = res.get("error")
        if isinstance(err, dict):
            return str(err.get("code") or ""), str(err.get("message") or "")
        return str(res.get("code") or ""), str(err or res.get("message") or "")

    def _call(self, *args: str, timeout: int = 60) -> tuple[dict, list[dict]]:
        """`mm`, signed in again once with `cli_token` if its session is gone; a
        refusal raised with MetaMask's own code and words."""
        res, notices = self._mm(*args, timeout=timeout)
        code, _ = self._error(res)
        if not res.get("ok", "error" not in res) and code in MM_SIGNED_OUT and self.secrets.get("cli_token"):
            login, _ = self._mm("login", env={"MM_CLI_TOKEN": self.secrets["cli_token"]})
            if login.get("ok", "error" not in login):
                res, notices = self._mm(*args, timeout=timeout)
        if not res.get("ok", "error" not in res):
            code, message = self._error(res)
            if code in MM_SIGNED_OUT:
                raise BackendError(f"MetaMask's mm is not signed in as the signer's user ({code}): its person "
                                   "runs mm login there")
            why = MM_REFUSALS.get(code) or message or "no reason given"
            held = next((n for n in notices if n.get("kind") == "AWAITING_MFA"), None)
            raise BackendError(f"MetaMask refused ({code or 'error'}): {self._redact(why)}"
                               + (f" (request {held.get('pollingId')})" if held and held.get("pollingId") else ""))
        return res, notices

    # ── the key work ─────────────────────────────────────────────────────────
    def public(self, agent_id: str, role: str, ref: str = "") -> str:
        if role != "evm":
            raise Unsupported(f"the {self.name} back end (metamask) holds EVM wallet keys only, not {role}")
        if not ref:
            raise BackendError(f"the {self.name} back end needs the MetaMask wallet's address for this key")
        from eth_utils import is_address, to_checksum_address
        if not is_address(ref):
            raise BackendError(f"{ref!r} is not an EVM address (a MetaMask wallet is named by its address)")
        return to_checksum_address(ref)

    def _limits(self, full_message: dict) -> int:
        """This back end's own limits; the chain id the request is for."""
        primary = str(full_message.get("primaryType") or "")
        if primary not in MM_PRIMARY_TYPES:
            raise Unsupported(f"the {self.name} back end (metamask) signs {' and '.join(MM_PRIMARY_TYPES)} "
                              f"only, not {primary or 'untyped data'}")
        domain = full_message.get("domain") or {}
        try:
            chain = int(domain["chainId"])
        except (KeyError, TypeError, ValueError):
            raise BackendError("MetaMask signs typed data for one chain; this names none") from None
        chains = self._chain_ids()
        if chain not in chains:
            raise BackendError(f"the {self.name} back end signs on chain {', '.join(map(str, sorted(chains)))} "
                               f"only, not {chain} (its chain_ids)")
        contracts = self._contracts(chains)
        if contracts is not None and str(domain.get("verifyingContract", "")).lower() not in contracts:
            raise BackendError(f"the {self.name} back end signs for its token contracts only, not "
                               f"{domain.get('verifyingContract')} (its verifying_contracts)")
        if (cap := self.settings.get("max_value")) not in (None, ""):
            value = int((full_message.get("message") or {}).get("value", 0))
            if value > int(cap):
                raise BackendError(f"the {self.name} back end signs at most {cap} base units, not {value} "
                                   "(its max_value)")
        return chain

    def _guarded(self, address: str) -> None:
        """MetaMask's own layer is there only in Guard Mode: refuse a wallet in Beast Mode."""
        import time
        if time.monotonic() - self._modes.get(address, -1e9) < 300:
            return
        res, _ = self._call("wallet", "trading-mode", "get", "--address", address)
        found: list[str] = []

        def walk(v: Any) -> None:
            if isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
            elif isinstance(v, str) and v.lower() in ("guard", "beast"):
                found.append(v.lower())
        walk(res.get("data"))
        if "beast" in found or not found:
            raise BackendError(f"the MetaMask wallet {address} is " + ("in Beast Mode" if found else
                               "in no trading mode mm would say") + ": its allowlists are not there, so it "
                               "signs nothing for Emporia — its person runs mm wallet trading-mode set guard")
        self._modes[address] = time.monotonic()

    def sign_typed_data(self, agent_id: str, full_message: dict, ref: str = "") -> bytes:
        self.require("evm-typed-data", "sign EIP-712 typed data")
        address = self.public(agent_id, "evm", ref)
        chain = self._limits(full_message)
        payload = json.dumps(_json_ready({"domain": full_message["domain"], "types": full_message["types"],
                                          "primaryType": full_message["primaryType"],
                                          "message": full_message["message"]}), separators=(",", ":"))
        message = full_message.get("message") or {}
        intent = (f"Emporia {agent_id}: {full_message['primaryType']} of {message.get('value')} to "
                  f"{message.get('to')}")[:200]
        wait = self._wait()
        with self._LOCK:
            self._guarded(address)
            self._call("wallet", "select", address)
            res, _ = self._call("wallet", "sign-typed-data", "--chain-id", str(chain), "--payload", payload,
                                "--wait", "--wallet-timeout", str(wait), "--intent", intent, timeout=wait + 30)
        try:
            sig = bytes.fromhex(str((res.get("data") or {})["signature"]).removeprefix("0x"))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise BackendError("MetaMask's answer carried no signature") from None
        if len(sig) != 65 or recover_typed_data(full_message, sig) != address:
            raise BackendError("MetaMask's signature is not from the wallet it named")
        return sig

    def make(self, agent_id: str, role: str) -> str:
        self.require("make-keys", "make keys")
        if role != "evm":
            raise Unsupported(f"the {self.name} back end (metamask) makes EVM wallets only, not {role}")
        import re
        name = re.sub(r"[^A-Za-z0-9-]", "-", f"emporia-{agent_id}")[:36]
        with self._LOCK:
            res, _ = self._call("wallet", "create", "--chain-namespace", "evm", "--name", name,
                                "--trading-mode", "guard")
        data = res.get("data") or {}
        address = data.get("address") or (data.get("wallet") or {}).get("address") if isinstance(data, dict) else None
        from eth_utils import is_address, to_checksum_address
        if not address or not is_address(address):
            raise BackendError("MetaMask's answer carried no wallet address: make it with mm wallet create, then "
                               f"emporia signer backend {self.name} use {agent_id} evm ADDRESS")
        return to_checksum_address(address)


def _p256_key(b64_der: str, what: str):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        priv = serialization.load_der_private_key(base64.b64decode(b64_der), password=None)
    except ValueError as e:
        raise BackendError(f"{what} is not a P-256 key (base64 PKCS#8)") from e
    if not isinstance(priv, ec.EllipticCurvePrivateKey):
        raise BackendError(f"{what} is not a P-256 key")
    return priv


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt(header: dict, claims: dict, secret: str, what: str, es256: bool = False) -> str:
    """A compact JWS: EdDSA with a base64 Ed25519 key (CDP's API keys), or ES256
    with an EC key (PEM, or base64 PKCS#8 — its wallet secret). ES256's signature
    is r‖s, 64 bytes, as JWS has it (not DER)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    if es256 or secret.strip().startswith("-----BEGIN"):
        if secret.strip().startswith("-----BEGIN"):
            try:
                priv = serialization.load_pem_private_key(secret.encode(), password=None)
            except ValueError as e:
                raise BackendError(f"{what} is not an EC key") from e
        else:
            priv = _p256_key(secret, what)
        alg = "ES256"
    else:
        try:
            raw = base64.b64decode(secret)
            priv = ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])
        except ValueError as e:
            raise BackendError(f"{what} is not an Ed25519 key (base64)") from e
        alg = "EdDSA"
    signing_input = (_b64url(json.dumps({**header, "alg": alg}, separators=(",", ":")).encode()) + "." +
                     _b64url(json.dumps(claims, separators=(",", ":")).encode()))
    if alg == "EdDSA":
        sig = priv.sign(signing_input.encode())
    else:
        r, s = decode_dss_signature(priv.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return signing_input + "." + _b64url(sig)


# ─── settings: which back ends, and which keys each agent's roles use ─────────

# What `emporia signer backend NAME add TYPE …` accepts; the rest is refused.
SETTINGS = {"vault": (), "aws-kms": ("region", "profile"), "privy": ("app_id", "policy_id", "owner_id", "base_url"),
            "cdp": ("api_key_id", "policy_id", "base_url"),
            "metamask": ("chain_ids", "verifying_contracts", "max_value", "wait_seconds", "mm")}
SECRETS = {"vault": ("op_service_account_token", "bw_session"), "aws-kms": (), "privy": ("app_secret", "authorization_key"),
           "cdp": ("api_key_secret", "wallet_secret"), "metamask": ("cli_token",)}
_CLASSES = {"local": LocalBackend, "vault": VaultBackend, "aws-kms": KmsBackend, "privy": PrivyBackend, "cdp": CdpBackend,
            "metamask": MetaMaskBackend}
# Tests put fakes here: {back end name: Backend}. Never used outside tests.
OVERRIDES: dict[str, Backend] = {}


def make_backend(name: str, type_: str, settings: dict | None = None, secrets: dict | None = None) -> Backend:
    if type_ not in _CLASSES:
        raise BackendError(f"no back end of type {type_!r}: {', '.join(TYPES)}")
    return _CLASSES[type_](name, settings or {}, secrets or {})
