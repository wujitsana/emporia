"""The relay's x402 escrow operator key — who signs, chosen by the operator (P96).

Commerce Payments holds (AuthCaptureEscrow on Base) are captured and voided by
an *operator*: an address that submits those transactions and pays their gas.
It can capture a hold to the receiver the payer fixed, or release it; it
cannot send the money anywhere else. It is the one key a relay running x402
holds keys for, and it is a deliberate, scoped exception to "the relay holds
no wallet" — fees go to a configured *address* (a Safe, ideally), which needs
no key on this machine at all.

Who signs is a choice, `EMPORIA_OPERATOR_SIGNER`:

    env       the key comes from EMPORIA_OPERATOR_PRIVATE_KEY (so a secret
              manager — Bitwarden Secrets Manager, 1Password — can inject it),
              else from the key file `generate` writes. Testnet default.
    aws-kms   planned: the key never leaves AWS KMS (ECC_SECG_P256K1). Named
              here so choosing it fails loudly rather than falling back.

Two guards, both before any money moves:

* `EMPORIA_OPERATOR_ADDRESS`, when set, must be the signer's address — the
  wrong key, or the wrong KMS key, is refused at load.
* A local key on a mainnet network needs `EMPORIA_ALLOW_HOT_OPERATOR_KEY=1`.

The key is never generated implicitly. Unlike the relay's federation identity,
which is created on first start, a key that pays gas exists only because
someone ran:

    python -m emporia.operator_signer generate

`eth-account` comes from the `x402` extra; it is imported only when a signer is
actually loaded, so a relay without x402 never needs it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Loaded before `identity`, which reads EMPORIA_KEYS_DIR when imported: the
# profile's .env is merged on import, so the CLI resolves the key directory
# exactly as the relay does instead of whatever this shell happens to export.
from emporia import config as _config  # noqa: F401
from emporia import identity

KEY_FILE_NAME = "relay_operator.secp256k1"

#: CAIP-2 networks where a hot key moves real money.
MAINNET_NETWORKS = frozenset({"eip155:1", "eip155:8453", "eip155:10", "eip155:42161",
                              "eip155:137"})

SIGNER_BACKENDS = ("env", "aws-kms")


class OperatorSignerError(RuntimeError):
    """The operator signer cannot be used as configured. The message says why."""


def key_path() -> Path:
    return identity.KEY_DIR / KEY_FILE_NAME


@dataclass(frozen=True)
class LocalKeySigner:
    """A secp256k1 key held by this process. Never logged, never returned."""

    _account: object

    @property
    def address(self) -> str:
        return self._account.address

    @property
    def backend(self) -> str:
        return "env"

    def sign_typed_data(self, full_message: dict) -> str:
        return "0x" + self._account.sign_typed_data(full_message=full_message).signature.hex()

    def sign_transaction(self, tx: dict) -> bytes:
        return bytes(self._account.sign_transaction(tx).raw_transaction)


def _git_work_tree(path: Path) -> Path | None:
    """The git working tree `path` sits inside, if any."""
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return parent
    return None


def _git_ignores(repo: Path, path: Path) -> bool:
    """Does git itself say `path` is ignored in `repo`? False when it cannot
    say — no git binary, not a real repository — since an unverified "probably
    ignored" is exactly how a key gets committed."""
    import subprocess

    try:
        return subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", str(path)],
            capture_output=True, timeout=10,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def generate_key() -> str:
    """Create the key file (0600 from creation) and return its address.

    Refuses to overwrite: replacing an operator key strands any hold it
    authorized as operator, so that has to be a deliberate act. And it never
    writes where git could pick it up: inside a git working tree, the key path
    must be one git confirms it ignores. The usual key directory is such a
    case (a profile home tracked in git, with `.hermes/` ignored), which is
    why the rule asks git rather than refusing every repository outright.
    """
    from eth_account import Account

    path = key_path()
    repo = _git_work_tree(path.parent.resolve())
    if repo is not None and not _git_ignores(repo, path.resolve()):
        raise OperatorSignerError(
            f"{path} is inside the git working tree {repo}, and git does not confirm it is "
            f"ignored. An operator key must never be committable — ignore its directory, "
            f"or point EMPORIA_KEYS_DIR outside the repository."
        )
    if path.exists():
        raise OperatorSignerError(
            f"{path} already exists. An operator key is not replaced implicitly — "
            f"holds it is operator for could no longer be captured or voided."
        )
    identity.KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    account = Account.create()
    identity._write_secret_atomically(path, bytes(account.key))
    return account.address


def _local_signer() -> LocalKeySigner:
    from eth_account import Account

    raw_hex = (os.getenv("EMPORIA_OPERATOR_PRIVATE_KEY") or "").strip()
    if raw_hex:
        try:
            return LocalKeySigner(Account.from_key(raw_hex))
        except Exception:
            # The value is not repeated back: it is a private key.
            raise OperatorSignerError(
                "EMPORIA_OPERATOR_PRIVATE_KEY is set but is not a valid secp256k1 "
                "private key (expected 32 bytes of hex)."
            ) from None
    path = key_path()
    if not path.exists():
        raise OperatorSignerError(
            f"No operator key: EMPORIA_OPERATOR_PRIVATE_KEY is unset and {path} does not "
            f"exist. Create one with `python -m emporia.operator_signer generate`."
        )
    raw = path.read_bytes()
    if len(raw) != 32:
        raise OperatorSignerError(
            f"{path} is {len(raw)} bytes, expected 32 — refusing to use or replace it. "
            f"Restore it from backup."
        )
    return LocalKeySigner(Account.from_key(raw))


def load_operator_signer(network: str = "") -> LocalKeySigner:
    """The configured signer, after both guards. `network` is the CAIP-2 id the
    holds will be on; pass it so a hot key on mainnet is caught here."""
    backend = (os.getenv("EMPORIA_OPERATOR_SIGNER") or "env").strip().lower()
    if backend == "aws-kms":
        raise OperatorSignerError(
            "EMPORIA_OPERATOR_SIGNER=aws-kms is planned but not built yet. Use 'env' on "
            "testnet; KMS support lands before mainnet."
        )
    if backend != "env":
        raise OperatorSignerError(
            f"Unknown EMPORIA_OPERATOR_SIGNER={backend!r}; choose one of "
            f"{', '.join(SIGNER_BACKENDS)}."
        )
    signer = _local_signer()

    expected = (os.getenv("EMPORIA_OPERATOR_ADDRESS") or "").strip()
    if expected and expected.lower() != signer.address.lower():
        raise OperatorSignerError(
            f"The operator key belongs to {signer.address}, but EMPORIA_OPERATOR_ADDRESS is "
            f"{expected}. Refusing to sign with the wrong key."
        )
    if network in MAINNET_NETWORKS and os.getenv("EMPORIA_ALLOW_HOT_OPERATOR_KEY") != "1":
        raise OperatorSignerError(
            f"{network} is a mainnet, and the operator key is a local hot key. Use a KMS "
            f"signer, or set EMPORIA_ALLOW_HOT_OPERATOR_KEY=1 to accept that risk explicitly."
        )
    return signer


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["generate"]:
        try:
            address = generate_key()
        except OperatorSignerError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"Operator key written to {key_path()} (0600).")
        print(f"Address: {address}")
        print("Fund it with a little test ETH for gas, and set "
              f"EMPORIA_OPERATOR_ADDRESS={address} so the relay can check it.")
        return 0
    if args[:1] == ["address"]:
        try:
            print(load_operator_signer().address)
        except OperatorSignerError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0
    print("usage: python -m emporia.operator_signer generate | address", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
