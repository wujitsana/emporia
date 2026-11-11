"""An agent's own x402 wallet, for paying from the MCP server (P104).

The Python SDK takes an x402 key as a constructor argument. The MCP server —
how a Hermes agent actually pays — had no wallet at all, so no MCP payment
tool could pay: x402 reached agents only through the SDK.

This gives each agent one secp256k1 key, `<agent>.x402.secp256k1`, in the
profile key directory, under the same rules as the operator key (P96): created
only by an explicit command, 0600 from creation, never where git would track it.

Before it signs anything it applies the agent's own policy, because a relay is
a counterparty, not a trusted party:

* **Networks** — `EMPORIA_X402_PAY_NETWORKS` (CAIP-2, comma-separated) must
  list the network. Required: an unset allowlist signs nothing, rather than
  whatever network a relay offers. P177: and only testnets count, unless a
  person has unlocked mainnet for this agent (`EMPORIA_X402_MAINNET`).
* **Amount** — at most `EMPORIA_X402_MAX_PAYMENT_CENTS` per payment (default
  1000, $10). A relay that asks for more is refused before signing.
* **Escrow offers** additionally go through `escrow.sign_offer`'s checks (the
  amount the session states, a fee cap).
* **Daily total** (P115) — at most `EMPORIA_X402_MAX_DAILY_CENTS` (default
  5000, $50) signed in any 24 hours. The per-payment cap alone let a relay ask
  for $10 again and again. Counted when signed, not when settled: a signed
  authorization is money the holder can collect.

    python -m emporia.agent_wallet generate --agent-id <id>
    python -m emporia.agent_wallet address --agent-id <id>
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from emporia import identity
from emporia.operator_signer import _git_ignores, _git_work_tree

DEFAULT_MAX_PAYMENT_CENTS = 1_000
DEFAULT_MAX_DAILY_CENTS = 5_000
_DAY_S = 24 * 3600
_LEDGER_LOCK = threading.Lock()
_USDC_ATOMIC_PER_CENT = 10_000      # 6 decimals


class AgentWalletError(RuntimeError):
    """The wallet cannot pay this, and the message says why."""


def positive_atomic(value: Any, what: str = "amount") -> int:
    """`value` as a whole number of atomic units greater than zero, or AgentWalletError. A zero or negative
    amount would add nothing (or less than nothing) to the spend ledgers, and a fraction is no amount."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AgentWalletError(f"not paid: the {what} is not a whole number of atomic units")
    text = str(value).strip()
    if not text.isascii() or not text.isdigit():
        raise AgentWalletError(f"not paid: the {what} {text[:24]!r} is not a positive whole number")
    n = int(text)
    if n <= 0:
        raise AgentWalletError(f"not paid: the {what} must be more than zero")
    return n


# P203: where the rules and keys come from. In the agent's own process (testnet,
# the default) they are its environment and its key directory. In the signer
# (emporia.signer) each request runs in a Scope: that agent's policy and keys as
# the signer keeps them, which the agent can neither read nor change.
@dataclass(frozen=True)
class Scope:
    env: dict
    key_dir: Path
    approver: Callable | None = None
    # P293 (signer phase 2): in the signer, the keys by their back ends — families,
    # addresses and signers (emporia.signer.Wallet). None: the files in key_dir.
    wallet: Any = None


_SCOPE: contextvars.ContextVar[Scope | None] = contextvars.ContextVar("emporia_wallet_scope", default=None)


@contextlib.contextmanager
def scope(s: Scope):
    token = _SCOPE.set(s)
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _getenv(name: str, default: str = "") -> str:
    s = _SCOPE.get()
    return s.env.get(name, default) if s is not None else os.getenv(name, default)


def _key_base(agent_id: str) -> Path:
    s = _SCOPE.get()
    return s.key_dir / f"{identity._keyfile_stem(agent_id)}.priv" if s is not None else identity._key_path(agent_id)


def _scoped_wallet():
    s = _SCOPE.get()
    return s.wallet if s is not None else None


def evm_signer(agent_id: str):
    """What signs for this agent's EVM wallet: in the signer, its back end's
    (`emporia.signer.Wallet`); else a signer around the key here. `.address`,
    `.sign_typed_data(full_message)`, `.sign_transaction(tx)`."""
    if (w := _scoped_wallet()) is not None:
        return w.evm()
    from eth_account import Account

    from emporia.operator_signer import LocalKeySigner
    return LocalKeySigner(Account.from_key(load_key(agent_id)))


def solana_signer(agent_id: str):
    """What signs for this agent's Solana wallet: its back end's in the signer, else its seed here."""
    if (w := _scoped_wallet()) is not None:
        return w.svm()
    return _load_solana_seed(agent_id)


def remote():
    """The signer client when this agent's wallet keys are behind the signer
    (EMPORIA_SIGNER_URL); None in the signer itself and for in-process wallets."""
    if _SCOPE.get() is not None or not os.getenv("EMPORIA_SIGNER_URL", "").strip():
        return None
    from emporia import signer_client
    return signer_client.Client.from_env()


def wallet_path(agent_id: str) -> Path:
    return _key_base(agent_id).with_name(_key_base(agent_id).stem + ".x402.secp256k1")


def generate(agent_id: str) -> str:
    """Create the agent's wallet key and return its address. Refuses to
    overwrite one, and refuses anywhere git would track it."""
    from eth_account import Account

    path = wallet_path(agent_id)
    repo = _git_work_tree(path.parent.resolve())
    if repo is not None and not _git_ignores(repo, path.resolve()):
        raise AgentWalletError(
            f"{path} is inside the git working tree {repo}, and git does not confirm it is "
            f"ignored. A wallet key must never be committable.")
    if path.exists():
        raise AgentWalletError(f"{path} already exists; a wallet key is not replaced implicitly.")
    account = Account.create()
    _keep(agent_id, "evm", bytes(account.key))
    return account.address


def _keep(agent_id: str, family: str, raw: bytes) -> None:
    """Store a new wallet key: in the profile .env when the agent keeps its keys
    there (P165), else a 0600 file."""
    from emporia import key_source
    if key_source.store_in_env():
        if source_for(family):
            raise AgentWalletError(f"{agent_id} already has a {family} wallet key; it is not replaced implicitly.")
        key_source.save_key_to_env(family, env_value(family, raw))
        return
    path = wallet_path(agent_id) if family == "evm" else solana_wallet_path(agent_id)
    identity.KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity._write_secret_atomically(path, raw)


def env_value(family: str, raw: bytes) -> str:
    """A wallet key as written to a .env — the form wallet apps import: EVM a
    0x hex key, Solana the base58 keypair Phantom exports (P175: it was hex,
    which the Solana parser could not read back)."""
    if family == "evm":
        return "0x" + raw.hex()
    from solders.keypair import Keypair
    return str(Keypair.from_seed(raw))


def solana_wallet_path(agent_id: str) -> Path:
    """P155: the agent's Solana key (a 32-byte ed25519 seed), beside its EVM one."""
    return _key_base(agent_id).with_name(_key_base(agent_id).stem + ".x402.ed25519")


def generate_solana(agent_id: str) -> str:
    """Create the agent's Solana wallet and return its address — same rules as
    the EVM one: explicit, 0600, never where git would track it."""
    import secrets as _secrets

    from emporia import x402_svm
    path = solana_wallet_path(agent_id)
    repo = _git_work_tree(path.parent.resolve())
    if repo is not None and not _git_ignores(repo, path.resolve()):
        raise AgentWalletError(f"{path} is inside the git working tree {repo}, and git does not "
                               f"confirm it is ignored. A wallet key must never be committable.")
    if path.exists():
        raise AgentWalletError(f"{path} already exists; a wallet key is not replaced implicitly.")
    seed = _secrets.token_bytes(32)
    _keep(agent_id, "svm", seed)
    return x402_svm.address_of(seed)


# P161: one recovery phrase can hold both wallets — the standard BIP-39 phrase a
# wallet app uses, derived on the paths MetaMask (EVM) and Phantom (Solana) use,
# so importing the phrase there shows the same two addresses. The derived keys
# are stored like any wallet key (files or the .env), so signing is unchanged.
# P175: the phrase is the backup, and a backup belongs off the machine. It is
# shown once to the person setting up, who writes it down, and is then not
# kept. Only when nobody could be shown it (setup run by an agent, or with no
# terminal) is it kept beside the keys until `emporia wallet backup` shows it
# and removes it. The agent's identity key is never a wallet:
# rotating identity must not move money, and an identity signature must never be
# a payment signature.
EVM_PATH = "m/44'/60'/0'/0/0"
SOLANA_PATH = "m/44'/501'/0'/0'"


def phrase_path(agent_id: str) -> Path:
    return _key_base(agent_id).with_name(_key_base(agent_id).stem + ".x402.phrase")


def has_phrase(agent_id: str) -> bool:
    return phrase_path(agent_id).exists() or bool(_getenv(PHRASE_SOURCE_VAR).strip())


def new_phrase() -> str:
    """A new 12-word BIP-39 recovery phrase."""
    from eth_account.hdaccount import generate_mnemonic
    from eth_account.types import Language
    return generate_mnemonic(12, Language.ENGLISH)


def derive_from_phrase(phrase: str) -> tuple[bytes, bytes]:
    """(EVM private key, Solana ed25519 seed) for a BIP-39 phrase."""
    from eth_account import Account
    from eth_account.hdaccount import Mnemonic
    from solders.keypair import Keypair

    phrase = " ".join(phrase.split()).lower()
    if not Mnemonic().is_mnemonic_valid(phrase):
        raise AgentWalletError("that is not a valid BIP-39 recovery phrase (check the words and their order)")
    Account.enable_unaudited_hdwallet_features()
    evm_key = bytes(Account.from_mnemonic(phrase, account_path=EVM_PATH).key)
    sol_seed = bytes(Keypair.from_seed_and_derivation_path(Mnemonic.to_seed(phrase), SOLANA_PATH))[:32]
    return evm_key, sol_seed


def generate_from_phrase(agent_id: str, phrase: str | None = None,
                         families: tuple[str, ...] = ("evm", "svm"),
                         keep_phrase: bool = True) -> dict[str, str]:
    """Create the agent's wallets from one recovery phrase — a new 12-word one,
    or `phrase` to import an existing wallet. The keys are always stored; the
    phrase too only with `keep_phrase` (P175: when nobody has written it down
    yet). Returns {family: address}. Refuses if anything it would write
    exists; nothing is written unless all can be."""
    from eth_account import Account

    from emporia import x402_svm
    targets = {"evm": wallet_path(agent_id), "svm": solana_wallet_path(agent_id)}
    paths = [*([phrase_path(agent_id)] if keep_phrase else []), *(targets[f] for f in families)]
    for path in paths:
        repo = _git_work_tree(path.parent.resolve())
        if repo is not None and not _git_ignores(repo, path.resolve()):
            raise AgentWalletError(f"{path} is inside the git working tree {repo}, and git does not "
                                   f"confirm it is ignored. A wallet key must never be committable.")
        if path.exists():
            raise AgentWalletError(f"{path} already exists; a wallet key is not replaced implicitly.")
    from emporia import key_source
    if key_source.store_in_env() and (os.getenv(PHRASE_SOURCE_VAR, "").strip()
                                      or any(source_for(f) for f in families)):
        # P173: in the .env there are no files for the checks above to find; a
        # phrase or key already there is the only copy, so it is never replaced.
        raise AgentWalletError(f"{agent_id} already has a wallet key or phrase in its .env; "
                               f"it is not replaced implicitly.")
    phrase = " ".join((phrase or new_phrase()).split()).lower()
    evm_key, sol_seed = derive_from_phrase(phrase)
    raws = {"evm": evm_key, "svm": sol_seed}
    for fam in families:      # the keys, as any wallet key (a file or the .env)
        _keep(agent_id, fam, raws[fam])
    if keep_phrase:           # after the keys: in the .env a phrase would count as both
        if key_source.store_in_env():
            key_source.save_key_to_env("phrase", phrase)
        else:
            identity._write_secret_atomically(phrase_path(agent_id), (phrase + "\n").encode())
    _SOURCE_CACHE.clear()
    out = {}
    if "evm" in families:
        out["evm"] = Account.from_key(evm_key).address
    if "svm" in families:
        out["svm"] = x402_svm.address_of(sol_seed)
    return out


def add_from_phrase(agent_id: str, family: str, phrase: str | None = None) -> str:
    """Make the wallet `family` is missing from the agent's phrase, so an agent
    that began with one wallet gets the other under the same backup. The
    phrase is the stored one, or (P175: it is not normally kept) `phrase` as
    typed again — which must give the wallet the agent already has."""
    from eth_account import Account

    from emporia import x402_svm
    other = "svm" if family == "evm" else "evm"
    if phrase is None:
        if not has_phrase(agent_id):
            raise AgentWalletError(f"{agent_id} keeps no recovery phrase; type it again to add the "
                                   f"other wallet under it, or create that wallet with its own key.")
        # P173: the phrase may be a file or linked (the .env, a password manager).
        phrase = (phrase_path(agent_id).read_text() if phrase_path(agent_id).exists()
                  else read_source(os.getenv(PHRASE_SOURCE_VAR, "")))
    evm_key, sol_seed = derive_from_phrase(phrase)
    raws = {"evm": evm_key, "svm": sol_seed}
    try:
        current = (address if other == "evm" else solana_address)(agent_id)
    except AgentWalletError:
        current = None
    mine = Account.from_key(evm_key).address if other == "evm" else x402_svm.address_of(sol_seed)
    if current is not None and current != mine:
        raise AgentWalletError("that phrase does not give the wallet this agent already has; "
                               "check the words and their order")
    if (wallet_path(agent_id) if family == "evm" else solana_wallet_path(agent_id)).exists() \
            or (os.getenv(KEY_SOURCE_VARS[family], "").strip()):
        raise AgentWalletError(f"{agent_id} already has a {family} wallet key; it is not replaced implicitly.")
    _keep(agent_id, family, raws[family])
    _SOURCE_CACHE.clear()
    return Account.from_key(evm_key).address if family == "evm" else x402_svm.address_of(sol_seed)


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(text: str) -> bytes:
    """Base58 (Bitcoin alphabet), as Solana wallets export secrets."""
    n = 0
    for ch in text:
        if ch not in _B58:
            raise AgentWalletError("a Solana secret key is base58, or a solana-keygen JSON array")
        n = n * 58 + _B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(text) - len(text.lstrip("1"))) + body


def _parse_secret(family: str, secret: str) -> bytes:
    """The 32 bytes a key file holds, from what wallets export: EVM — a hex
    private key; Solana — a base58 secret (Phantom's 64-byte export, or a 32-byte
    seed) or solana-keygen's JSON array of 64 numbers."""
    import json as _json

    text = secret.strip()
    if family == "evm":
        raw = text.removeprefix("0x")
        if len(raw) != 64 or any(c not in "0123456789abcdefABCDEF" for c in raw):
            raise AgentWalletError("an EVM private key is 64 hex characters (optionally 0x…)")
        return bytes.fromhex(raw)
    if len(text) == 64 and all(c in "0123456789abcdefABCDEF" for c in text):
        return bytes.fromhex(text)    # a 32-byte seed as hex (how P165 first stored it)
    if text.startswith("["):
        try:
            data = bytes(_json.loads(text))
        except (ValueError, TypeError) as e:
            raise AgentWalletError("a solana-keygen key file is a JSON array of 64 numbers") from e
    else:
        data = _b58decode(text)
    if len(data) == 64:
        from solders.keypair import Keypair
        kp = Keypair.from_bytes(data)          # checks the public half matches
        return bytes(kp)[:32]
    if len(data) == 32:
        return data
    raise AgentWalletError(f"a Solana secret key is 32 or 64 bytes, this is {len(data)}")


def import_key(agent_id: str, family: str, secret: str) -> str:
    """Bring in an existing wallet's key as this agent's `family` wallet.
    Same rules as creating one: never replaces a key, 0600, never where git
    tracks it. Returns the address."""
    from eth_account import Account

    from emporia import x402_svm
    path = wallet_path(agent_id) if family == "evm" else solana_wallet_path(agent_id)
    repo = _git_work_tree(path.parent.resolve())
    if repo is not None and not _git_ignores(repo, path.resolve()):
        raise AgentWalletError(f"{path} is inside the git working tree {repo}, and git does not "
                               f"confirm it is ignored. A wallet key must never be committable.")
    if path.exists():
        raise AgentWalletError(f"{path} already exists; a wallet key is not replaced implicitly.")
    raw = _parse_secret(family, secret)
    _keep(agent_id, family, raw)
    return Account.from_key(raw).address if family == "evm" else x402_svm.address_of(raw)


# P162: a key can stay where the operator already keeps it — an environment
# variable, 1Password, Bitwarden, or anything with a CLI that prints a secret —
# and be read only when the wallet signs. Nothing is written to disk. The
# setting is a reference, never the secret:
#   env:NAME                         an environment variable the agent's process has
#   cmd:op read op://Vault/Item/key  1Password CLI (OP_SERVICE_ACCOUNT_TOKEN, unattended)
#   cmd:bw get password ITEM         Bitwarden CLI (BW_SESSION)
# One per wallet, or one recovery phrase for both. It wins over a key file.
KEY_SOURCE_VARS = {"evm": "EMPORIA_X402_EVM_KEY_SOURCE", "svm": "EMPORIA_X402_SOLANA_KEY_SOURCE"}
PHRASE_SOURCE_VAR = "EMPORIA_X402_PHRASE_SOURCE"
_SOURCE_CACHE: dict[tuple[str, str], bytes] = {}


def read_source(ref: str) -> str:
    """The secret a reference points at (see key_source). Errors name the source, never the value."""
    from emporia.key_source import KeySourceError, read_text
    try:
        return read_text(ref)
    except KeySourceError as e:
        raise AgentWalletError(str(e)) from e


def source_for(family: str) -> str:
    return (_getenv(KEY_SOURCE_VARS[family]).strip()
            or (f"phrase:{_getenv(PHRASE_SOURCE_VAR).strip()}"
                if _getenv(PHRASE_SOURCE_VAR).strip() else ""))


def secret_from_source(family: str, ref: str | None = None) -> bytes | None:
    """The 32 key bytes from the configured source, or None if none is set.
    Read once per process."""
    ref = source_for(family) if ref is None else ref
    if not ref:
        return None
    if (family, ref) not in _SOURCE_CACHE:
        if ref.startswith("phrase:"):
            evm, sol = derive_from_phrase(read_source(ref.removeprefix("phrase:")))
            raw = evm if family == "evm" else sol
        else:
            raw = _parse_secret(family, read_source(ref))
        _SOURCE_CACHE[(family, ref)] = raw
    return _SOURCE_CACHE[(family, ref)]


def _load_solana_seed(agent_id: str) -> bytes:
    linked = secret_from_source("svm")
    if linked is not None:
        return linked
    path = solana_wallet_path(agent_id)
    if not path.exists():
        raise AgentWalletError(f"{agent_id} has no Solana wallet. Create one with "
                               f"`python -m emporia.agent_wallet generate --solana --agent-id {agent_id}`.")
    raw = path.read_bytes()
    if len(raw) != 32:
        raise AgentWalletError(f"{path} is {len(raw)} bytes, expected 32; restore it from backup.")
    return raw


def solana_address(agent_id: str) -> str:
    if (r := remote()) is not None:
        return r.wallet()["solana_address"]
    if (w := _scoped_wallet()) is not None:
        return w.solana_address()
    from emporia import x402_svm
    return x402_svm.address_of(_load_solana_seed(agent_id))


def _families(agent_id: str) -> tuple[str, ...]:
    """The networks families this agent holds a key for."""
    if (r := remote()) is not None:
        return tuple(r.wallet()["families"])
    if (w := _scoped_wallet()) is not None:
        return w.families()
    return tuple(f for f, p in (("evm", wallet_path(agent_id)), ("svm", solana_wallet_path(agent_id)))
                 if p.exists() or source_for(f))


def load_key(agent_id: str) -> str:
    """The wallet's private key as hex. Never logged or returned to a caller
    outside this module's signing helpers."""
    linked = secret_from_source("evm")
    if linked is not None:
        return "0x" + linked.hex()
    path = wallet_path(agent_id)
    if not path.exists():
        raise AgentWalletError(
            f"{agent_id} has no x402 wallet. Create one with "
            f"`python -m emporia.agent_wallet generate --agent-id {agent_id}`, fund it "
            f"with USDC, and register it with register_x402_wallet.")
    raw = path.read_bytes()
    if len(raw) != 32:
        raise AgentWalletError(f"{path} is {len(raw)} bytes, expected 32; restore it from backup.")
    return "0x" + raw.hex()


def address(agent_id: str) -> str:
    if (r := remote()) is not None:
        return r.wallet()["address"]
    if (w := _scoped_wallet()) is not None:
        return w.address()
    from eth_account import Account
    return Account.from_key(load_key(agent_id)).address


def allowed_networks() -> list[str]:
    if (r := remote()) is not None:
        return r.wallet()["pay_networks"]
    nets = [n.strip() for n in _getenv("EMPORIA_X402_PAY_NETWORKS").split(",") if n.strip()]
    if not nets:
        raise AgentWalletError(
            "EMPORIA_X402_PAY_NETWORKS is not set, so this agent pays on no network. Set it "
            "to the CAIP-2 ids it may pay on, e.g. eip155:84532 for Base Sepolia.")
    from emporia.networks import is_testnet, label, mainnet_unlocked
    s = _SCOPE.get()
    unlocked = mainnet_unlocked(s.env if s is not None else None)
    if unlocked and s is None and os.getenv("EMPORIA_ALLOW_HOT_AGENT_WALLET", "").strip() != "1":
        # P203: real money only from behind the signer — here the key is in the
        # agent's own process, which its tools may reach.
        unlocked = False
        if not any(is_testnet(n) for n in nets):
            raise AgentWalletError(
                "mainnet is unlocked for this agent, but its wallet keys are in its own process. Real money "
                "is paid only from behind the signer: its operator runs `emporia signer add NAME`.")
    if not unlocked:
        live = [n for n in nets if not is_testnet(n)]
        nets = [n for n in nets if is_testnet(n)]
        if not nets:
            raise AgentWalletError(
                f"this agent pays on testnets only — mainnet (real money) is locked — and every network "
                f"it lists is mainnet ({', '.join(label(n) for n in live)}). Only its operator can "
                f"unlock mainnet, in their own terminal: emporia wallet mainnet NAME.")
    return nets


def max_payment_cents() -> int:
    if (r := remote()) is not None:
        return r.wallet()["max_payment_cents"]
    raw = _getenv("EMPORIA_X402_MAX_PAYMENT_CENTS").strip()
    return int(raw) if raw else DEFAULT_MAX_PAYMENT_CENTS


def max_daily_cents() -> int:
    if (r := remote()) is not None:
        return r.wallet()["max_daily_cents"]
    raw = _getenv("EMPORIA_X402_MAX_DAILY_CENTS").strip()
    return int(raw) if raw else DEFAULT_MAX_DAILY_CENTS


def spent_today_atomic(agent_id: str) -> int:
    """USDC (atomic) this agent has signed for in the last 24 hours."""
    if (r := remote()) is not None:
        return r.wallet(fresh=True)["spent_today_atomic"]
    return ledger_spent(agent_id)


def _ledger_path(agent_id: str) -> Path:
    path = wallet_path(agent_id)
    return path.with_name(path.name.removesuffix(".secp256k1") + ".spend.json")


# P166: above a threshold the operator decides, not the agent. The MCP server
# sets APPROVER to a function that asks them (an MCP elicitation, which Hermes
# routes to its approval prompts — Telegram, Slack, the CLI). It returns True
# (approved), False (declined) or None (no way to ask here); None refuses.
APPROVER = None


def approval_threshold_cents() -> int | None:
    raw = _getenv("EMPORIA_PAYMENT_APPROVAL_CENTS").strip()
    return int(raw) if raw else None


def _exact(amount_atomic: int) -> str:
    """An amount in whole units, to the last atomic unit (6 decimals for USDC) —
    an approval prompt never rounds 5.004 to 5.00."""
    text = f"{amount_atomic / 1e6:.6f}"
    while text.endswith("0") and len(text.split(".")[1]) > 2:
        text = text[:-1]
    return text


def terms_digest(terms: dict) -> str:
    """P265: a short hash of the terms that decide where a payment's money goes.
    It is in the approval message, and the signer matches an approval on the
    message, so an approval is for these terms exactly — not for another
    payment that reads the same."""
    import hashlib
    return hashlib.sha256(json.dumps(terms, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def plain_text(text: Any, limit: int = 400) -> str:
    """Printable ASCII only, no control or ANSI characters, at most `limit` characters: what a person may be
    shown of a string an agent or a relay supplied (S-7)."""
    out = "".join(c if 32 <= ord(c) < 127 else "?" for c in str(text))
    return out if len(out) <= limit else out[:limit - 3] + "..."


#: The terms of the payment being put to the approver, as this wallet computed them (S-7): the amount, rail,
#: network and payee are shown from here, not from the free text an agent or relay could shape.
_APPROVAL_TERMS: contextvars.ContextVar[dict | None] = contextvars.ContextVar("emporia_approval_terms", default=None)


def approval_terms() -> dict:
    return dict(_APPROVAL_TERMS.get() or {})


def require_approval(agent_id: str, amount_atomic: int, what: str, terms: dict | None = None) -> None:
    """Ask the operator before a payment above EMPORIA_PAYMENT_APPROVAL_CENTS.
    Runs after the caps, so nothing above a cap is ever put to them."""
    limit = approval_threshold_cents()
    if limit is None or amount_atomic <= limit * _USDC_ATOMIC_PER_CENT:
        return
    bound = f" Terms {terms_digest(terms)}." if terms else ""
    message = (f"Emporia: {agent_id} wants to pay {_exact(amount_atomic)} {plain_text(what)}.{bound} "
               f"Above your ${limit / 100:.2f} approval threshold. Approve this payment?")
    s = _SCOPE.get()
    approver = s.approver if s is not None else APPROVER
    t = terms or {}
    token = _APPROVAL_TERMS.set({
        "amount_atomic": int(amount_atomic), "rail": plain_text(t.get("rail", ""), 20),
        "network": plain_text(t.get("network", ""), 60),
        "pay_to": plain_text(t.get("pay_to") or t.get("recipient") or t.get("receiver") or "", 80)})
    try:
        answer = approver(message) if approver else None
    finally:
        _APPROVAL_TERMS.reset(token)
    if answer is None:
        raise AgentWalletError(
            f"this payment ({amount_atomic / 1e6:.2f}) needs your operator's approval (above "
            f"${limit / 100:.2f}, EMPORIA_PAYMENT_APPROVAL_CENTS) and this agent's app cannot ask "
            f"them. Not paid.")
    if not answer:
        raise AgentWalletError("your operator declined this payment. Not paid; don't retry it.")


@contextlib.contextmanager
def _ledger_held(agent_id: str):
    """The 24-hour spend log, held against this process's threads and (P275)
    every other process on this machine that pays for the same agent — its MCP
    server and an SDK script share one daily cap. The lock is on the key
    folder, so no lock file sits among the wallet's key files."""
    path = _ledger_path(agent_id)
    with _LEDGER_LOCK:
        fd = None
        try:
            import fcntl
            fd = os.open(path.parent, os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except (ImportError, OSError):     # no flock here (Windows), or no folder yet: this process only
            if fd is not None:
                os.close(fd)
            fd = None
        try:
            try:
                entries = json.loads(path.read_text()) if path.exists() else []
            except (OSError, ValueError):
                entries = []
            now = time.time()
            # S-2: only positive entries count (and are kept): a negative one would lift the daily cap
            yield path, [[t, a] for t, a in entries if now - t < _DAY_S and isinstance(a, int) and a > 0]
        finally:
            if fd is not None:
                os.close(fd)          # closing the descriptor releases the lock


def ledger_spent(agent_id: str) -> int:
    """P275: what the spend log on this machine says was signed in 24 hours —
    the in-process total, with no signer asked (the SDK's)."""
    with _ledger_held(agent_id) as (_, entries):
        return sum(a for _, a in entries if a > 0)


def charge_daily(agent_id: str, amount_atomic: int, cap_atomic: int) -> None:
    """Refuse (AgentWalletError) if this payment would take the last 24 hours'
    signed total past `cap_atomic`; otherwise record it. Called just before
    signing, by the MCP wallet and (P275) the SDK."""
    amount_atomic = positive_atomic(amount_atomic)
    with _ledger_held(agent_id) as (path, entries):
        spent = sum(a for _, a in entries if a > 0)      # a poisoned (non-positive) entry counts for nothing
        if spent + amount_atomic > cap_atomic:
            raise AgentWalletError(
                f"this payment ({amount_atomic / 1e6:.2f} USDC) would take today's signed total "
                f"past this agent's {cap_atomic / 1e6:.2f} USDC daily cap "
                f"({spent / 1e6:.2f} already; EMPORIA_X402_MAX_DAILY_CENTS)")
        entries.append([time.time(), amount_atomic])
        identity._write_secret_atomically(path, json.dumps(entries).encode())


def refund_daily(agent_id: str, amount_atomic: int) -> None:
    """P275: take back the newest charge of `amount_atomic` — the payment was
    refused after it was charged, before anything was signed."""
    from emporia import autonomy
    if _SCOPE.get() is None:          # in the signer there is no agent turn to count (see _charge_daily)
        autonomy.refund_spend(amount_atomic)
    with _ledger_held(agent_id) as (path, entries):
        for i in range(len(entries) - 1, -1, -1):
            if entries[i][1] == amount_atomic:
                del entries[i]
                identity._write_secret_atomically(path, json.dumps(entries).encode())
                return


def _charge_daily(agent_id: str, amount_atomic: int) -> None:
    """The MCP wallet's every payment passes here, just before signing: in a turn nobody at the keyboard
    started it must also fit the playbook's autonomous daily allowance (none by default; unsure = unattended).

    Money-safety S-1: that allowance is the agent's, counted in the agent's own process (here, or in
    `_remote_gate` before a hand-off to a signer). Inside a signer (a Scope) this process's own environment
    says nothing about whose turn it is, so the signer never applies it; it keeps its per-payment and daily
    caps, which hold whatever the agent says."""
    from emporia import autonomy
    amount_atomic = positive_atomic(amount_atomic)
    gated = _SCOPE.get() is None
    if gated and (why := autonomy.gate_spend(amount_atomic)):
        raise AgentWalletError(why)
    try:
        charge_daily(agent_id, amount_atomic, max_daily_cents() * _USDC_ATOMIC_PER_CENT)
    except AgentWalletError:
        if gated:
            autonomy.refund_spend(amount_atomic)
        raise


@contextlib.contextmanager
def _remote_gate(amount_atomic: int):
    """S-1: before a paying call is handed to a signer, count it against this agent's unattended-turn
    allowance in this process, where the turn is known (a person's turn is never limited here). Given back
    when the signer refuses or fails; the signer's own caps are the second wall."""
    from emporia import autonomy
    amount_atomic = positive_atomic(amount_atomic)
    if why := autonomy.gate_spend(amount_atomic):
        raise AgentWalletError(why)
    try:
        yield
    except BaseException:
        autonomy.refund_spend(amount_atomic)
        raise


def _or_refund(agent_id: str, amount_atomic: int, sign):
    """`sign()`, charged already; the charge given back if it raises. Review pass 2: a back
    end that would not sign (a provider's policy, MetaMask's Guard Mode or its 2FA timing
    out, a signature from another key) or an offer refused at signing left its amount on the
    day's total, so every refusal used up the daily cap. Only for signatures, which nobody
    holds when this raises; a Tempo payment sends a transaction, and keeps its charge."""
    try:
        return sign()
    except BaseException:
        refund_daily(agent_id, amount_atomic)
        raise


def _payee_label(statement: Any, family: str, address: Any) -> str:
    """P262: refuse unless `address` is a wallet its payee signed (`payee.check`);
    the payee's name for the approval prompt. Runs here so the signer checks too."""
    from emporia import payee
    try:
        return payee.check(statement, family, str(address or ""))
    except payee.PayeeError as e:
        raise AgentWalletError(f"not paid: {e}") from None


def _offered_amount(body: dict, network: str = "") -> int:
    """The most an x402 body asks for (on `network` when given): what a hand-off to a signer is counted
    against the unattended allowance as, before the signer picks the requirement it will pay."""
    from emporia import x402_payer
    accepts = x402_payer.payment_required_from(body).get("accepts") or []
    amounts = [positive_atomic(e.get("amount"), "amount the relay asks for") for e in accepts
               if isinstance(e, dict) and e.get("scheme") == "exact" and (not network or e.get("network") == network)]
    if not amounts:
        raise AgentWalletError("nothing offered here can be paid on that network")
    return max(amounts)


def _regate_actual(header: str, counted: int) -> None:
    """The signer paid less than the most the body asked for: count what it signed, not the maximum."""
    from emporia import autonomy
    from emporia.payments import extract_x402_payload
    try:
        actual = int(((extract_x402_payload(header) or {}).get("accepted") or {}).get("amount") or 0)
    except (TypeError, ValueError):
        return
    if 0 < actual < counted:
        autonomy.refund_spend(counted)
        autonomy.gate_spend(actual)


def sign_exact(agent_id: str, body: dict, network: str = "", *, fee_leg: bool = False,
               creator_amount: int = 0) -> str:
    """A relay's x402 402 body in, the PAYMENT-SIGNATURE value out — after the
    network allowlist and the per-payment cap.

    P160: which network pays is the agent's choice, not the relay's listing
    order: `network` if given (it must be on the allowlist), else the first
    network in EMPORIA_X402_PAY_NETWORKS that the relay offers and this agent
    has a wallet for.

    `fee_leg`: the second payment of a paid entry, to the relay's published fee address (fees.py): `body`
    holds the fee requirement and `creator_amount` (atomic) the creator's share it goes with. It is paid
    only on the network the creator's share was, never above `PAYER_FEE_CAP_BPS` of the whole price, and
    under the same per-payment and daily caps and approval as any payment."""
    if (r := remote()) is not None:
        hint = _offered_amount(body, network)          # S-1: the allowance is counted here, then the signer's caps
        with _remote_gate(hint):
            header = r.call("x402", {"body": body, "network": network, "fee_leg": fee_leg,
                                     "creator_amount": creator_amount})["header"]
        _regate_actual(header, hint)
        return header
    from emporia import x402_payer

    nets = allowed_networks()
    if network and network not in nets:
        raise AgentWalletError(
            f"{network} is not one of the networks this agent may pay on "
            f"(EMPORIA_X402_PAY_NETWORKS: {', '.join(nets)})")
    challenge = x402_payer.payment_required_from(body)
    families = _families(agent_id) or ("evm",)
    requirement, refusals = None, []
    for net in ([network] if network else nets):
        try:
            requirement = x402_payer.select_requirement(challenge, networks=[net], families=families)
            break
        except x402_payer.X402PayerError as e:
            refusals.append(str(e))
    if requirement is None:
        raise x402_payer.X402PayerError("nothing offered here can be paid: " + "; ".join(refusals))
    amount = positive_atomic(requirement.get("amount"), "amount the relay asks for")
    cap = max_payment_cents() * _USDC_ATOMIC_PER_CENT
    if amount > cap:
        raise AgentWalletError(
            f"the relay asks for {amount / 1e6:.2f} USDC, above this agent's "
            f"{cap / 1e6:.2f} USDC per-payment cap (EMPORIA_X402_MAX_PAYMENT_CENTS)")
    from emporia.networks import label
    from emporia.payments import x402_network_family
    if fee_leg:
        from emporia.fees import PAYER_FEE_CAP_BPS
        if not creator_amount or amount * 10_000 > (amount + creator_amount) * PAYER_FEE_CAP_BPS:
            raise AgentWalletError(
                f"not paid: the relay's fee of {amount / 1e6:.4f} USDC is more than {PAYER_FEE_CAP_BPS / 100:g}% "
                f"of the price ({(amount + creator_amount) / 1e6:.4f} USDC)")
        payee = "the relay (its published fee address)"
    else:
        # P262: the address is the payee's own, signed by it — not just the relay's word.
        payee = _payee_label(body.get("payee") if isinstance(body, dict) else None,
                             x402_network_family(requirement.get("network") or "") or "evm",
                             requirement.get("payTo"))
    # P206: the whole payee and what is bought — an approval (the signer's) is for exactly this payment.
    resource = (challenge.get("resource") or {}).get("url") or "a resource"
    require_approval(agent_id, amount, f"USDC on {label(requirement.get('network') or '')} to "
                                       f"{payee}, {requirement.get('payTo')}, for {resource}",
                     {"rail": "x402", "network": requirement.get("network"), "asset": requirement.get("asset"),
                      "pay_to": str(requirement.get("payTo") or "").lower(), "amount": str(amount),
                      "extra": requirement.get("extra"), "resource": resource})
    if x402_network_family(requirement.get("network") or "") == "svm":
        # P155: Solana — a partially signed transaction, the facilitator pays fees.
        from emporia import x402_svm
        seed = solana_signer(agent_id)
        _charge_daily(agent_id, amount)
        return _or_refund(agent_id, amount, lambda: x402_payer.payment_signature_header(
            x402_svm.build_payment_payload(requirement, seed, resource=challenge.get("resource"))))
    key = evm_signer(agent_id)
    _charge_daily(agent_id, amount)
    return _or_refund(agent_id, amount, lambda: x402_payer.payment_signature_header(
        x402_payer.build_payment_payload(requirement, key, resource=challenge.get("resource"))))


def sign_entry(agent_id: str, body: dict, network: str = "") -> dict[str, str]:
    """A relay's 402 body in, the payment headers out: PAYMENT-SIGNATURE, and for a paid entry that carries
    the relay's fee (`x402_fee`, fees.py) also PAYMENT-SIGNATURE-FEE, signed on the same network. Both are
    signed or neither is: a refused fee leg gives the creator's daily charge back."""
    from emporia.payments import extract_x402_payload
    header = sign_exact(agent_id, body, network)
    headers = {"PAYMENT-SIGNATURE": header}
    fee = body.get("x402_fee") if isinstance(body, dict) else None
    if not fee:
        return headers
    payload = extract_x402_payload(header) or {}
    accepted = payload.get("accepted") or {}
    try:
        headers["PAYMENT-SIGNATURE-FEE"] = sign_exact(
            agent_id, {"x402": fee}, accepted.get("network") or "", fee_leg=True,
            creator_amount=int(accepted.get("amount") or 0))
    except BaseException:
        refund_daily(agent_id, int(accepted.get("amount") or 0))
        raise
    return headers


def _check_tempo_offer(network: str, request: dict) -> None:
    """S-3: the chain a Tempo transfer is signed for and the token it moves are the allowed network's,
    never the offer's own say: an offer labelled with a testnet is not a mainnet transfer."""
    from emporia.networks import NETWORKS
    known = NETWORKS.get(network)
    try:
        chain_id = int(network.split(":", 1)[1])
        offered_chain = int((request.get("methodDetails") or {}).get("chainId"))
    except (IndexError, TypeError, ValueError):
        raise AgentWalletError("not paid: the Tempo offer names no readable chain id") from None
    if offered_chain != chain_id:
        raise AgentWalletError(f"not paid: the offer is for network {network} but asks to be signed for chain "
                               f"{offered_chain}")
    tokens = {known.usdc.lower()} if known and known.usdc else set()
    if _getenv("EMPORIA_TEMPO_NETWORK").strip() == network and (own := _getenv("EMPORIA_TEMPO_TOKEN").strip()):
        tokens.add(own.lower())
    if str(request.get("currency") or "").lower() not in tokens:
        raise AgentWalletError(f"not paid: the offer asks for token {str(request.get('currency'))[:44]!r}, "
                               f"which is not the stablecoin this client knows for {network}")


def pay_tempo(agent_id: str, offer: dict) -> str:
    """P156: pay a relay's Tempo offer from this agent's EVM wallet — a TIP-20
    transfer straight to the payee — after the same network allowlist,
    per-payment cap and daily cap as x402. Returns the PAYMENT-TEMPO value."""
    if (r := remote()) is not None:
        with _remote_gate(positive_atomic((offer.get("request") or {}).get("amount"), "amount the relay asks for")):
            return r.call("tempo", {"offer": offer})["header"]
    from emporia import tempo

    network = offer.get("network") or ""
    if network not in allowed_networks():
        raise AgentWalletError(f"{network} is not in EMPORIA_X402_PAY_NETWORKS, so this agent "
                               f"does not pay there")
    amount = positive_atomic(offer["request"].get("amount"), "amount the relay asks for")
    _check_tempo_offer(network, offer["request"])
    cap = max_payment_cents() * _USDC_ATOMIC_PER_CENT
    if amount > cap:
        raise AgentWalletError(
            f"the relay asks for {amount / 1e6:.2f}, above this agent's {cap / 1e6:.2f} "
            f"per-payment cap (EMPORIA_X402_MAX_PAYMENT_CENTS)")
    from emporia.networks import label
    payee = _payee_label(offer.get("payee"), "evm", offer["request"].get("recipient"))   # P262
    request = offer["request"]
    require_approval(agent_id, amount, f"in stablecoin {request.get('currency')} on {label(network)} to "
                                       f"{payee}, {request.get('recipient')}",
                     {"rail": "tempo", "network": network, "currency": request.get("currency"),
                      "recipient": str(request.get("recipient") or "").lower(), "amount": str(amount),
                      "chain_id": (request.get("methodDetails") or {}).get("chainId")})
    key = evm_signer(agent_id)
    _charge_daily(agent_id, amount)
    try:
        return tempo.pay(offer, key)
    except tempo.TempoNotSent:
        refund_daily(agent_id, amount)      # nothing was broadcast, so nothing can be collected: not spent
        raise


def _escrow_contracts(network: str) -> dict:
    """S-4: the escrow and collector contracts this agent's person set for `network` (and no others)."""
    if _getenv("EMPORIA_ESCROW_NETWORK").strip() != network:
        return {}
    return {"escrow": _getenv("EMPORIA_ESCROW_CONTRACT"), "collector": _getenv("EMPORIA_ESCROW_COLLECTOR")}


def sign_escrow(agent_id: str, offer: dict, *, expected_amount: int) -> str:
    """A relay's escrow offer in, the PAYMENT-ESCROW value out — after the
    network allowlist, the per-payment cap, and escrow.sign_offer's checks."""
    if (r := remote()) is not None:
        with _remote_gate(positive_atomic(expected_amount, "hold amount")):
            return r.call("escrow", {"offer": offer, "expected_amount": expected_amount})["header"]
    from emporia import escrow

    expected_amount = positive_atomic(expected_amount, "hold amount")
    if expected_amount > max_payment_cents() * _USDC_ATOMIC_PER_CENT:
        raise AgentWalletError(
            f"the hold is {expected_amount / 1e6:.2f} USDC, above this agent's per-payment cap")
    key, networks = evm_signer(agent_id), allowed_networks()
    from emporia.networks import label
    info = offer.get("payment_info") or {}
    receiver = info.get("receiver")
    payee = _payee_label(offer.get("payee"), "evm", receiver)   # P262
    # P265: who may capture it and what fee it may take decide where the money goes:
    # in the prompt and in the terms the approval is for (not the salt or times,
    # which every fresh offer changes).
    require_approval(agent_id, expected_amount,
                     f"USDC into an escrow hold on {label(offer.get('network') or '')} (contract "
                     f"{offer.get('escrow')}, for {payee}, {receiver}; capture by {info.get('operator')}, "
                     f"fee {info.get('min_fee_bps')}-{info.get('max_fee_bps')} bps to {info.get('fee_receiver')})",
                     {"rail": "escrow", "network": offer.get("network"), "chain_id": offer.get("chain_id"),
                      "escrow": str(offer.get("escrow") or "").lower(), "token": str(offer.get("token") or "").lower(),
                      "collector": str(offer.get("collector") or "").lower(),
                      **{k: str(info.get(k) or "").lower() for k in ("receiver", "operator", "fee_receiver")},
                      **{k: str(info.get(k) or "") for k in ("max_amount", "min_fee_bps", "max_fee_bps")}})
    _charge_daily(agent_id, expected_amount)
    return _or_refund(agent_id, expected_amount, lambda: escrow.sign_offer(
        offer, key, expected_amount=expected_amount, allowed_networks=networks,
        contracts=_escrow_contracts(offer.get("network") or "")))


def reclaim_escrow(agent_id: str, hold: dict) -> dict:
    """Take back an expired escrow hold this agent funded: a transaction to the
    escrow contract, from its wallet. {"transaction", "reclaimed_atomic", "network"}."""
    if (r := remote()) is not None:
        return r.call("reclaim", {"hold": hold})
    import asyncio

    from emporia import escrow, escrow_chain, networks
    offer = hold["offer"]
    network = str(offer.get("network") or "")
    # S-4: a reclaim sends a transaction to a contract from the wallet: only on a test network this agent
    # may pay on, to the contract its person named.
    if network not in allowed_networks() or not networks.is_testnet(network):
        raise AgentWalletError(f"not reclaimed: {network or 'that network'} is not a test network this agent pays on")
    pinned = _escrow_contracts(network).get("escrow", "").strip().lower()
    if not pinned or str(offer.get("escrow") or "").strip().lower() != pinned:
        raise AgentWalletError("not reclaimed: the hold's escrow contract is not the one this agent was told to "
                               "trust (EMPORIA_ESCROW_NETWORK, EMPORIA_ESCROW_CONTRACT)")
    info = escrow.PaymentInfo.from_json({**offer["payment_info"], "payer": hold["payer"]})
    cfg = escrow_chain.payer_config(offer)
    if not cfg.rpc_url:
        raise AgentWalletError(f"no RPC for chain {cfg.chain_id}: set EMPORIA_X402_RPC_URL")
    state = asyncio.run(escrow_chain.payment_state(cfg, info))
    if not state["capturable"]:
        raise AgentWalletError("nothing left to reclaim — the hold was captured, voided or already reclaimed")
    if time.time() < info.authorization_expiry:
        raise AgentWalletError("too early: the contract allows a reclaim from " + time.strftime(
            "%Y-%m-%d %H:%M UTC", time.gmtime(info.authorization_expiry)))
    tx = asyncio.run(escrow_chain.reclaim(cfg, evm_signer(agent_id), info))
    return {"transaction": tx, "reclaimed_atomic": state["capturable"], "network": cfg.network}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="An agent's x402 wallet")
    parser.add_argument("command", choices=["generate", "generate-phrase", "derive", "import-key", "address"])
    parser.add_argument("--agent-id", default=os.getenv("EMPORIA_AGENT_ID", ""))
    parser.add_argument("--solana", action="store_true", help="the Solana wallet (P155), not the EVM one")
    parser.add_argument("--families", default="evm,svm",
                        help="generate-phrase: which wallets the phrase makes (evm,svm)")
    parser.add_argument("--import-stdin", action="store_true",
                        help="generate-phrase, derive: read the phrase from stdin (never from argv)")
    parser.add_argument("--keep-phrase", action="store_true",
                        help="generate-phrase: keep the phrase beside the keys (the default for a "
                             "new phrase nobody has seen; one read from stdin is not kept)")
    args = parser.parse_args(argv)
    if not args.agent_id:
        print("error: --agent-id (or EMPORIA_AGENT_ID) is required", file=sys.stderr)
        return 2
    try:
        if args.command == "generate-phrase":
            phrase = sys.stdin.readline() if args.import_stdin else None
            fams = tuple(f.strip() for f in args.families.split(",") if f.strip())
            keep = args.keep_phrase or not args.import_stdin
            made = generate_from_phrase(args.agent_id, phrase, fams, keep_phrase=keep)
            print("Recovery phrase kept beside the keys until it is backed up (emporia wallet backup)."
                  if keep else "Keys stored; the recovery phrase was not kept.")
            for fam, addr in made.items():
                print(f"Address ({fam}): {addr}")
            return 0
        if args.command == "import-key":   # the secret arrives on stdin, never argv
            addr = import_key(args.agent_id, "svm" if args.solana else "evm", sys.stdin.read())
            print(f"Address: {addr}")
            return 0
        if args.command == "derive":
            addr = add_from_phrase(args.agent_id, "svm" if args.solana else "evm",
                                   sys.stdin.readline() if args.import_stdin else None)
            print(f"Address: {addr}")
            return 0
        if args.command == "generate":
            addr = generate_solana(args.agent_id) if args.solana else generate(args.agent_id)
            where = solana_wallet_path(args.agent_id) if args.solana else wallet_path(args.agent_id)
            print(f"Wallet written to {where} (0600).")
            print(f"Address: {addr}")
            print("Fund it with USDC, then register it as the agent's payout wallet "
                  "(register_x402_wallet) so it can also be paid.")
        else:
            print(solana_address(args.agent_id) if args.solana else address(args.agent_id))
    except AgentWalletError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
