"""An agent's own x402 wallet, for paying from the MCP server (P104).

The Python SDK takes an x402 key as a constructor argument. The MCP server —
how a Hermes agent actually pays — had no wallet at all, so every MCP payment
tool was Stripe-only and x402 reached agents only through the SDK.

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

import json
import os
import sys
import threading
import time
from pathlib import Path

from emporia import identity
from emporia.operator_signer import _git_ignores, _git_work_tree

DEFAULT_MAX_PAYMENT_CENTS = 1_000
DEFAULT_MAX_DAILY_CENTS = 5_000
_DAY_S = 24 * 3600
_LEDGER_LOCK = threading.Lock()
_USDC_ATOMIC_PER_CENT = 10_000      # 6 decimals


class AgentWalletError(RuntimeError):
    """The wallet cannot pay this, and the message says why."""


def wallet_path(agent_id: str) -> Path:
    return identity._key_path(agent_id).with_name(
        identity._key_path(agent_id).stem + ".x402.secp256k1")


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
    return identity._key_path(agent_id).with_name(
        identity._key_path(agent_id).stem + ".x402.ed25519")


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
    return identity._key_path(agent_id).with_name(identity._key_path(agent_id).stem + ".x402.phrase")


def has_phrase(agent_id: str) -> bool:
    return phrase_path(agent_id).exists() or bool(os.getenv(PHRASE_SOURCE_VAR, "").strip())


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
    return (os.getenv(KEY_SOURCE_VARS[family], "").strip()
            or (f"phrase:{os.getenv(PHRASE_SOURCE_VAR, '').strip()}"
                if os.getenv(PHRASE_SOURCE_VAR, "").strip() else ""))


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
    from emporia import x402_svm
    return x402_svm.address_of(_load_solana_seed(agent_id))


def _families(agent_id: str) -> tuple[str, ...]:
    """The networks families this agent holds a key for."""
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
    from eth_account import Account
    return Account.from_key(load_key(agent_id)).address


def allowed_networks() -> list[str]:
    nets = [n.strip() for n in os.getenv("EMPORIA_X402_PAY_NETWORKS", "").split(",") if n.strip()]
    if not nets:
        raise AgentWalletError(
            "EMPORIA_X402_PAY_NETWORKS is not set, so this agent pays on no network. Set it "
            "to the CAIP-2 ids it may pay on, e.g. eip155:84532 for Base Sepolia.")
    from emporia.networks import is_testnet, label, mainnet_unlocked
    if not mainnet_unlocked():
        live = [n for n in nets if not is_testnet(n)]
        nets = [n for n in nets if is_testnet(n)]
        if not nets:
            raise AgentWalletError(
                f"this agent pays on testnets only — mainnet (real money) is locked — and every network "
                f"it lists is mainnet ({', '.join(label(n) for n in live)}). Only its operator can "
                f"unlock mainnet, in their own terminal: emporia wallet mainnet NAME.")
    return nets


def max_payment_cents() -> int:
    raw = os.getenv("EMPORIA_X402_MAX_PAYMENT_CENTS", "").strip()
    return int(raw) if raw else DEFAULT_MAX_PAYMENT_CENTS


def max_daily_cents() -> int:
    raw = os.getenv("EMPORIA_X402_MAX_DAILY_CENTS", "").strip()
    return int(raw) if raw else DEFAULT_MAX_DAILY_CENTS


def spent_today_atomic(agent_id: str) -> int:
    """USDC (atomic) this agent has signed for in the last 24 hours."""
    try:
        entries = json.loads(_ledger_path(agent_id).read_text())
    except (OSError, ValueError):
        return 0
    now = time.time()
    return sum(a for t, a in entries if now - t < _DAY_S)


def _ledger_path(agent_id: str) -> Path:
    path = wallet_path(agent_id)
    return path.with_name(path.name.removesuffix(".secp256k1") + ".spend.json")


# P166: above a threshold the operator decides, not the agent. The MCP server
# sets APPROVER to a function that asks them (an MCP elicitation, which Hermes
# routes to its approval prompts — Telegram, Slack, the CLI). It returns True
# (approved), False (declined) or None (no way to ask here); None refuses.
APPROVER = None


def approval_threshold_cents() -> int | None:
    raw = os.environ.get("EMPORIA_PAYMENT_APPROVAL_CENTS", "").strip()
    return int(raw) if raw else None


def require_approval(agent_id: str, amount_atomic: int, what: str) -> None:
    """Ask the operator before a payment above EMPORIA_PAYMENT_APPROVAL_CENTS.
    Runs after the caps, so nothing above a cap is ever put to them."""
    limit = approval_threshold_cents()
    if limit is None or amount_atomic <= limit * _USDC_ATOMIC_PER_CENT:
        return
    message = (f"Emporia: {agent_id} wants to pay {amount_atomic / 1e6:.2f} {what}. "
               f"Above your ${limit / 100:.2f} approval threshold. Approve this payment?")
    answer = APPROVER(message) if APPROVER else None
    if answer is None:
        raise AgentWalletError(
            f"this payment ({amount_atomic / 1e6:.2f}) needs your operator's approval (above "
            f"${limit / 100:.2f}, EMPORIA_PAYMENT_APPROVAL_CENTS) and this agent's app cannot ask "
            f"them. Not paid.")
    if not answer:
        raise AgentWalletError("your operator declined this payment. Not paid; don't retry it.")


def _charge_daily(agent_id: str, amount_atomic: int) -> None:
    """Refuse if this payment would take the last 24 hours' signed total past
    the daily cap; otherwise record it. Called just before signing."""
    cap = max_daily_cents() * _USDC_ATOMIC_PER_CENT
    path = _ledger_path(agent_id)
    with _LEDGER_LOCK:
        try:
            entries = json.loads(path.read_text()) if path.exists() else []
        except (OSError, ValueError):
            entries = []
        now = time.time()
        entries = [[t, a] for t, a in entries if now - t < _DAY_S]
        spent = sum(a for _, a in entries)
        if spent + amount_atomic > cap:
            raise AgentWalletError(
                f"this payment ({amount_atomic / 1e6:.2f} USDC) would take today's signed total "
                f"past this agent's {cap / 1e6:.2f} USDC daily cap "
                f"({spent / 1e6:.2f} already; EMPORIA_X402_MAX_DAILY_CENTS)")
        entries.append([now, amount_atomic])
        identity._write_secret_atomically(path, json.dumps(entries).encode())


def sign_exact(agent_id: str, body: dict, network: str = "") -> str:
    """A relay's x402 402 body in, the PAYMENT-SIGNATURE value out — after the
    network allowlist and the per-payment cap.

    P160: which network pays is the agent's choice, not the relay's listing
    order: `network` if given (it must be on the allowlist), else the first
    network in EMPORIA_X402_PAY_NETWORKS that the relay offers and this agent
    has a wallet for."""
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
    amount = int(requirement.get("amount") or 0)
    cap = max_payment_cents() * _USDC_ATOMIC_PER_CENT
    if amount > cap:
        raise AgentWalletError(
            f"the relay asks for {amount / 1e6:.2f} USDC, above this agent's "
            f"{cap / 1e6:.2f} USDC per-payment cap (EMPORIA_X402_MAX_PAYMENT_CENTS)")
    from emporia.networks import label
    require_approval(agent_id, amount, f"USDC on {label(requirement.get('network') or '')} to "
                                       f"{str(requirement.get('payTo'))[:12]}…")
    from emporia.payments import x402_network_family
    if x402_network_family(requirement.get("network") or "") == "svm":
        # P155: Solana — a partially signed transaction, the facilitator pays fees.
        from emporia import x402_svm
        seed = _load_solana_seed(agent_id)
        _charge_daily(agent_id, amount)
        return x402_payer.payment_signature_header(x402_svm.build_payment_payload(
            requirement, seed, resource=challenge.get("resource")))
    key = load_key(agent_id)
    _charge_daily(agent_id, amount)
    return x402_payer.payment_signature_header(x402_payer.build_payment_payload(
        requirement, key, resource=challenge.get("resource")))


def pay_tempo(agent_id: str, offer: dict) -> str:
    """P156: pay a relay's Tempo offer from this agent's EVM wallet — a TIP-20
    transfer straight to the payee — after the same network allowlist,
    per-payment cap and daily cap as x402. Returns the PAYMENT-TEMPO value."""
    from emporia import tempo

    network = offer.get("network") or ""
    if network not in allowed_networks():
        raise AgentWalletError(f"{network} is not in EMPORIA_X402_PAY_NETWORKS, so this agent "
                               f"does not pay there")
    amount = int(offer["request"]["amount"])
    cap = max_payment_cents() * _USDC_ATOMIC_PER_CENT
    if amount > cap:
        raise AgentWalletError(
            f"the relay asks for {amount / 1e6:.2f}, above this agent's {cap / 1e6:.2f} "
            f"per-payment cap (EMPORIA_X402_MAX_PAYMENT_CENTS)")
    from emporia.networks import label
    require_approval(agent_id, amount, f"in stablecoin on {label(network)} to "
                                       f"{str(offer['request'].get('recipient'))[:12]}…")
    key = load_key(agent_id)
    _charge_daily(agent_id, amount)
    return tempo.pay(offer, key)


def sign_escrow(agent_id: str, offer: dict, *, expected_amount: int) -> str:
    """A relay's escrow offer in, the PAYMENT-ESCROW value out — after the
    network allowlist, the per-payment cap, and escrow.sign_offer's checks."""
    from emporia import escrow

    if expected_amount > max_payment_cents() * _USDC_ATOMIC_PER_CENT:
        raise AgentWalletError(
            f"the hold is {expected_amount / 1e6:.2f} USDC, above this agent's per-payment cap")
    key, networks = load_key(agent_id), allowed_networks()
    from emporia.networks import label
    require_approval(agent_id, expected_amount, f"USDC into an escrow hold on "
                                                f"{label(offer.get('network') or '')}")
    _charge_daily(agent_id, expected_amount)
    return escrow.sign_offer(offer, key, expected_amount=expected_amount,
                             allowed_networks=networks)


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
