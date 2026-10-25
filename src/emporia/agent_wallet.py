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
  whatever network a relay offers.
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
    identity.KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    account = Account.create()
    identity._write_secret_atomically(path, bytes(account.key))
    return account.address


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
    identity.KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    seed = _secrets.token_bytes(32)
    identity._write_secret_atomically(path, seed)
    return x402_svm.address_of(seed)


# P161: one recovery phrase can hold both wallets — the standard BIP-39 phrase a
# wallet app uses, derived on the paths MetaMask (EVM) and Phantom (Solana) use,
# so importing the phrase there shows the same two addresses. The derived keys
# are written to the usual files, so signing is unchanged; the phrase is kept
# beside them (0600) as the backup. The agent's identity key is never a wallet:
# rotating identity must not move money, and an identity signature must never be
# a payment signature.
EVM_PATH = "m/44'/60'/0'/0/0"
SOLANA_PATH = "m/44'/501'/0'/0'"


def phrase_path(agent_id: str) -> Path:
    return identity._key_path(agent_id).with_name(identity._key_path(agent_id).stem + ".x402.phrase")


def has_phrase(agent_id: str) -> bool:
    return phrase_path(agent_id).exists()


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
                         families: tuple[str, ...] = ("evm", "svm")) -> dict[str, str]:
    """Create the agent's wallets from one recovery phrase — a new 12-word one,
    or `phrase` to import an existing wallet. Returns {family: address}. Refuses
    if any file it would write exists; nothing is written unless all can be."""
    from eth_account import Account
    from eth_account.hdaccount import generate_mnemonic
    from eth_account.types import Language

    from emporia import x402_svm
    targets = {"evm": wallet_path(agent_id), "svm": solana_wallet_path(agent_id)}
    paths = [phrase_path(agent_id), *(targets[f] for f in families)]
    for path in paths:
        repo = _git_work_tree(path.parent.resolve())
        if repo is not None and not _git_ignores(repo, path.resolve()):
            raise AgentWalletError(f"{path} is inside the git working tree {repo}, and git does not "
                                   f"confirm it is ignored. A wallet key must never be committable.")
        if path.exists():
            raise AgentWalletError(f"{path} already exists; a wallet key is not replaced implicitly.")
    phrase = " ".join((phrase or generate_mnemonic(12, Language.ENGLISH)).split()).lower()
    evm_key, sol_seed = derive_from_phrase(phrase)
    identity.KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity._write_secret_atomically(phrase_path(agent_id), (phrase + "\n").encode())
    out = {}
    if "evm" in families:
        identity._write_secret_atomically(targets["evm"], evm_key)
        out["evm"] = Account.from_key(evm_key).address
    if "svm" in families:
        identity._write_secret_atomically(targets["svm"], sol_seed)
        out["svm"] = x402_svm.address_of(sol_seed)
    return out


def add_from_phrase(agent_id: str, family: str) -> str:
    """Make the wallet `family` is missing from the agent's existing phrase, so
    an agent that began with one wallet gets the other under the same backup."""
    from eth_account import Account

    from emporia import x402_svm
    if not has_phrase(agent_id):
        raise AgentWalletError(f"{agent_id} has no recovery phrase; its wallets use separate keys.")
    target = wallet_path(agent_id) if family == "evm" else solana_wallet_path(agent_id)
    if target.exists():
        raise AgentWalletError(f"{target} already exists; a wallet key is not replaced implicitly.")
    evm_key, sol_seed = derive_from_phrase(phrase_path(agent_id).read_text())
    identity._write_secret_atomically(target, evm_key if family == "evm" else sol_seed)
    return Account.from_key(evm_key).address if family == "evm" else x402_svm.address_of(sol_seed)


def _load_solana_seed(agent_id: str) -> bytes:
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
                 if p.exists())


def load_key(agent_id: str) -> str:
    """The wallet's private key as hex. Never logged or returned to a caller
    outside this module's signing helpers."""
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
    _charge_daily(agent_id, expected_amount)
    return escrow.sign_offer(offer, key, expected_amount=expected_amount,
                             allowed_networks=networks)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="An agent's x402 wallet")
    parser.add_argument("command", choices=["generate", "generate-phrase", "derive", "address"])
    parser.add_argument("--agent-id", default=os.getenv("EMPORIA_AGENT_ID", ""))
    parser.add_argument("--solana", action="store_true", help="the Solana wallet (P155), not the EVM one")
    parser.add_argument("--families", default="evm,svm",
                        help="generate-phrase: which wallets the phrase makes (evm,svm)")
    parser.add_argument("--import-stdin", action="store_true",
                        help="generate-phrase: read an existing phrase from stdin (never from argv)")
    args = parser.parse_args(argv)
    if not args.agent_id:
        print("error: --agent-id (or EMPORIA_AGENT_ID) is required", file=sys.stderr)
        return 2
    try:
        if args.command == "generate-phrase":
            phrase = sys.stdin.readline() if args.import_stdin else None
            fams = tuple(f.strip() for f in args.families.split(",") if f.strip())
            made = generate_from_phrase(args.agent_id, phrase, fams)
            print(f"Recovery phrase written to {phrase_path(args.agent_id)} (0600).")
            for fam, addr in made.items():
                print(f"Address ({fam}): {addr}")
            return 0
        if args.command == "derive":
            addr = add_from_phrase(args.agent_id, "svm" if args.solana else "evm")
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
