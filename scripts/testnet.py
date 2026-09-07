#!/usr/bin/env python
"""Run the testnet lanes, with the lane config supplied rather than typed (P66).

`docs/testnet.md` is a good runbook and a bad command. It asks the operator to
export five variables by hand — including a USDC contract address that is
unrecoverable money if mistyped — and then relies on them to honour a rule
("never point EMPORIA_TESTNET_* at mainnet") that nothing enforces.

This supplies the known-good values, refuses anything that is not an
allowlisted testnet, tells you which lanes can run *before* running any of
them, and then runs exactly those.

    python scripts/testnet.py                 # preflight, then every runnable lane
    python scripts/testnet.py --check         # preflight only, run nothing
    python scripts/testnet.py --lane 1b       # one lane

Lane 3 is the only one that needs a funded key, and it is opt-in for that
reason (`--lane 3` or `--lane all`, plus EMPORIA_TESTNET_PAYER_KEY).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Base Sepolia. Deliberately not a default anywhere in `src/` — the relay's own
# x402 config must stay blank (ROADMAP § Payments), and this is the *harness's*
# copy, which is why the variable names differ from the relay's.
BASE_SEPOLIA = "eip155:84532"
BASE_SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
PUBLIC_FACILITATOR = "https://x402.org/facilitator"
BASE_SEPOLIA_RPC = "https://sepolia.base.org"

# The only chain ids this script will ever configure. A CAIP-2 id outside this
# set is refused rather than passed through: the failure mode being guarded is
# an operator pointing the harness at mainnet and signing a real transfer, and
# a typo'd chain id is indistinguishable from a deliberate one at the call site.
TESTNET_CHAIN_IDS = {
    "eip155:84532",    # Base Sepolia
    "eip155:11155111",  # Ethereum Sepolia
    "eip155:421614",   # Arbitrum Sepolia
    "eip155:11155420",  # OP Sepolia
}


@dataclass
class Lane:
    key: str
    title: str
    tests: list[str]
    needs: list[str] = field(default_factory=list)
    costs_money: bool = False
    note: str = ""


LANES = [
    Lane("1", "x402 signature verification (free, unfunded)",
         ["test_testnet_facilitator_accepts_a_credential_this_repo_signed",
          "test_testnet_facilitator_rejects_a_tampered_credential"],
         note="/verify only — checks a signature, moves nothing."),
    Lane("1b", "x402 chain reads (free, unfunded)",
         ["test_testnet_authorization_state_reads_the_real_chain",
          "test_testnet_authorization_log_recovers_the_transaction_that_spent_a_nonce",
          "test_testnet_the_log_window_is_a_bound_not_an_answer"],
         note="Public RPC reads. No key, no funds."),
    Lane("2", "Stripe PaymentIntent confirm (test mode)",
         ["test_testnet_stripe_confirm_reaches_a_real_endpoint"],
         needs=["EMPORIA_TESTNET_STRIPE_KEY"],
         note="Needs a real sk_test_ key. Never run since it was written."),
    Lane("3", "Stripe paid room join, end to end (test mode)",
         ["test_testnet_paid_room_join_settles_through_stripe"],
         needs=["EMPORIA_TESTNET_STRIPE_KEY"],
         note="Needs a real sk_test_ key. Never run since it was written."),
    Lane("settle", "x402 settlement (SPENDS testnet USDC)",
         [],  # selected by payer key presence; see docs/testnet.md
         needs=["EMPORIA_TESTNET_PAYER_KEY"], costs_money=True,
         note="Pays a 1-cent room to the payer's own address. Gas only."),
]


# A plain GET answered "ok (HTTP 403)" for the facilitator and "ok (HTTP 405)"
# for the RPC — reachable, and useless. Both were the request's shape, not the
# service's health, and the same code would have called a real 403 ("your
# requests are blocked") ready. Each check now asks the question its lane
# actually depends on.

def _http(url: str, *, data: bytes | None = None, timeout: float = 10.0):
    req = urllib.request.Request(  # noqa: S310
        url, data=data,
        headers={"User-Agent": "emporia-testnet-preflight",
                 **({"Content-Type": "application/json"} if data else {})},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.status, resp.read()


def check_facilitator(url: str, network: str) -> tuple[bool, str]:
    """Does this facilitator actually support the network we are about to use?"""
    import json
    try:
        status, body = _http(url.rstrip("/") + "/supported")
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if status != 200:
        return False, f"HTTP {status} from /supported"
    try:
        kinds = json.loads(body).get("kinds", [])
    except Exception:
        return False, "/supported did not return JSON"
    schemes = sorted({k.get("scheme", "") for k in kinds if k.get("network") == network})
    if not schemes:
        return False, f"does not support {network} (offers "\
                      f"{sorted({k.get('network','?') for k in kinds})})"
    return True, f"supports {network} — schemes {', '.join(schemes)}"


def check_rpc(url: str) -> tuple[bool, str]:
    """Answer a real JSON-RPC call, and say how far along the chain is."""
    import json
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
    ).encode()
    try:
        status, body = _http(url, data=payload)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if status != 200:
        return False, f"HTTP {status}"
    try:
        result = json.loads(body)["result"]
        return True, f"head at block {int(result, 16):,}"
    except Exception:
        return False, f"unexpected response: {body[:80]!r}"


def configure(network: str, asset: str, facilitator: str) -> None:
    if network not in TESTNET_CHAIN_IDS:
        raise SystemExit(
            f"Refusing to configure {network!r}: not in this script's testnet "
            f"allowlist ({', '.join(sorted(TESTNET_CHAIN_IDS))}).\n"
            "If that is genuinely a testnet, add it here deliberately. The "
            "check exists because a mistyped or mainnet chain id looks exactly "
            "like an intended one at the point where a transfer gets signed."
        )
    os.environ.setdefault("EMPORIA_TESTNET_FACILITATOR_URL", facilitator)
    os.environ.setdefault("EMPORIA_TESTNET_NETWORK", network)
    os.environ.setdefault("EMPORIA_TESTNET_ASSET", asset)
    os.environ.setdefault("EMPORIA_X402_RPC_EIP155_84532", BASE_SEPOLIA_RPC)


def preflight(facilitator: str) -> dict[str, tuple[bool, str]]:
    print("Preflight")
    results: dict[str, tuple[bool, str]] = {}
    network = os.environ["EMPORIA_TESTNET_NETWORK"]
    ok, detail = check_facilitator(facilitator, network)
    results["facilitator"] = (ok, detail)
    print(f"  facilitator  {'ok ' if ok else 'FAIL'}  {facilitator}")
    print(f"               {detail}")
    ok, detail = check_rpc(BASE_SEPOLIA_RPC)
    results["rpc"] = (ok, detail)
    print(f"  rpc          {'ok ' if ok else 'FAIL'}  {BASE_SEPOLIA_RPC}")
    print(f"               {detail}")
    print()
    return results


def lane_status(lane: Lane, checks: dict[str, tuple[bool, str]]) -> tuple[bool, str]:
    missing = [name for name in lane.needs if not os.getenv(name, "").strip()]
    if missing:
        return False, f"needs {', '.join(missing)}"
    if lane.key in ("1",) and not checks.get("facilitator", (False, ""))[0]:
        return False, "facilitator unreachable"
    if lane.key in ("1b",) and not checks.get("rpc", (False, ""))[0]:
        return False, "RPC unreachable"
    if lane.costs_money:
        return True, "ready — SPENDS testnet USDC"
    return True, "ready"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lane", default="runnable",
                        help="1, 1b, 2, 3, settle, all, or 'runnable' (default)")
    parser.add_argument("--check", action="store_true", help="preflight only")
    parser.add_argument("--network", default=BASE_SEPOLIA)
    parser.add_argument("--asset", default=BASE_SEPOLIA_USDC)
    parser.add_argument("--facilitator", default=PUBLIC_FACILITATOR)
    args = parser.parse_args()

    configure(args.network, args.asset, args.facilitator)
    print(f"Network {os.environ['EMPORIA_TESTNET_NETWORK']}  "
          f"asset {os.environ['EMPORIA_TESTNET_ASSET']}\n")

    checks = preflight(args.facilitator)

    print("Lanes")
    selected: list[Lane] = []
    for lane in LANES:
        ready, why = lane_status(lane, checks)
        mark = "✓" if ready else "·"
        print(f"  {mark} lane {lane.key:<7} {lane.title}")
        print(f"      {why}. {lane.note}")
        wanted = (
            args.lane == "all"
            or args.lane == lane.key
            or (args.lane == "runnable" and ready and not lane.costs_money)
        )
        if wanted and ready and lane.tests:
            selected.append(lane)
    print()

    if args.check:
        return 0
    if not selected:
        print("Nothing to run. Use --lane to name one explicitly.")
        return 0

    names = [t for lane in selected for t in lane.tests]
    print(f"Running {len(names)} test(s) from lane(s) "
          f"{', '.join(sorted({lane.key for lane in selected}))}\n")
    cmd = [sys.executable, "-m", "pytest", "-m", "testnet", "-q",
           "-k", " or ".join(names)]
    return subprocess.run(cmd, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
