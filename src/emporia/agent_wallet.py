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


def sign_exact(agent_id: str, body: dict) -> str:
    """A relay's x402 402 body in, the PAYMENT-SIGNATURE value out — after the
    network allowlist and the per-payment cap."""
    from emporia import x402_payer

    nets = allowed_networks()
    challenge = x402_payer.payment_required_from(body)
    requirement = x402_payer.select_requirement(challenge, networks=nets)
    amount = int(requirement.get("amount") or 0)
    cap = max_payment_cents() * _USDC_ATOMIC_PER_CENT
    if amount > cap:
        raise AgentWalletError(
            f"the relay asks for {amount / 1e6:.2f} USDC, above this agent's "
            f"{cap / 1e6:.2f} USDC per-payment cap (EMPORIA_X402_MAX_PAYMENT_CENTS)")
    key = load_key(agent_id)
    _charge_daily(agent_id, amount)
    return x402_payer.payment_signature_header(x402_payer.build_payment_payload(
        requirement, key, resource=challenge.get("resource")))


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
    parser.add_argument("command", choices=["generate", "address"])
    parser.add_argument("--agent-id", default=os.getenv("EMPORIA_AGENT_ID", ""))
    args = parser.parse_args(argv)
    if not args.agent_id:
        print("error: --agent-id (or EMPORIA_AGENT_ID) is required", file=sys.stderr)
        return 2
    try:
        if args.command == "generate":
            addr = generate(args.agent_id)
            print(f"Wallet written to {wallet_path(args.agent_id)} (0600).")
            print(f"Address: {addr}")
            print("Fund it with USDC, then register it as the agent's payout wallet "
                  "(register_x402_wallet) so it can also be paid.")
        else:
            print(address(args.agent_id))
    except AgentWalletError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
