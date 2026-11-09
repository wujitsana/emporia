#!/usr/bin/env python3
"""A real testnet money run: two live agent profiles, Emporia's own tools, USDC on Base Sepolia.

It does by script what two agents do by hand, with no model: each agent is a subprocess that runs
the Emporia MCP server's tool functions in the environment Hermes builds for that profile (the
same launch as `scripts/e2e.py`), against a relay that is already running. Nothing here reads or
prints a key, a phrase or an environment value: the values go from Hermes into the worker.

1. Paid room entry: alpha makes a paid room (a paid room is private: the relay refuses a public one) (5 cents, x402); beta joins, is told what to
   pay (`payment_required`), joins again paying with x402. Checked: beta is a member, and on chain
   a USDC transfer of exactly that amount from beta's address to alpha's, with its hash.
2. Service purchase: beta (the buyer makes the session) opens a service session, alpha joins as the
   seller, beta funds it. A purchase is paid by a held card charge or an x402 escrow hold; a relay
   with no escrow and a seller with no card account has no way to take it, and the run reports that
   as it is (the refusal the relay gives), then abandons the session. When funding works the run
   goes on: alpha delivers, beta confirms, the settlement record and the transfer are checked.

Safety, checked before any payment and again at each one: the network must be Base Sepolia
(eip155:84532) and the asset its USDC, or nothing is paid; every payment is below the approval
threshold of an agent's wallet (50 cents); the planned total must stay under the cap (50 cents).
A payment that fails stops the run: nothing is retried.

    .venv/bin/python scripts/money_run.py --dry-run     # everything but the payments
    .venv/bin/python scripts/money_run.py               # the real run, once

Profiles are named with --alpha / --beta (default `alpha`, `beta`) and found under the Hermes home
(HERMES_HOME, else ~/.hermes) as `profiles/<name>`, or given with --alpha-home / --beta-home.
Exit code 0 only when every step passed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

NETWORK = "eip155:84532"
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
EXPLORER = "https://sepolia.basescan.org"
DEFAULT_RPC = "https://sepolia.base.org"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ATOMIC_PER_CENT = 10_000          # USDC has 6 decimals
APPROVAL_FLOOR_CENTS = 50         # alpha's wallet asks above this: every payment stays below it
SPEND_CAP_CENTS = 50              # the whole run


class Refused(Exception):
    """A safety check said no: nothing was paid."""


class StepFailed(Exception):
    pass


# ── safety checks (pure, tested offline) ────────────────────────────────────────

def refuse_unless_testnet(network: str, asset: str) -> None:
    if network != NETWORK:
        raise Refused(f"network {network!r} is not Base Sepolia ({NETWORK}): refusing")
    if str(asset).lower() != USDC.lower():
        raise Refused(f"asset {asset!r} is not Base Sepolia USDC ({USDC}): refusing")


def check_requirement(req: dict[str, Any], expect_cents: int, pay_to: str) -> None:
    """One x402 `accepts` entry: Base Sepolia USDC, exactly the amount, paid to the payee's wallet."""
    refuse_unless_testnet(str(req.get("network") or ""), str(req.get("asset") or ""))
    want = expect_cents * ATOMIC_PER_CENT
    if int(req.get("amount") or 0) != want:
        raise Refused(f"the relay asks {req.get('amount')} atomic USDC, expected exactly {want}")
    if pay_to and str(req.get("payTo") or "").lower() != pay_to.lower():
        raise Refused("the payment would go to an address other than the payee's registered wallet")


@dataclass
class Spend:
    """The run's own account of what it has paid: no payment is made past the cap or the floor."""
    cap_cents: int = SPEND_CAP_CENTS
    spent_cents: int = 0

    def allow(self, cents: int) -> None:
        if cents <= 0:
            raise Refused(f"a payment of {cents} cents makes no sense")
        if cents >= APPROVAL_FLOOR_CENTS:
            raise Refused(f"{cents} cents is not below {APPROVAL_FLOOR_CENTS}: a wallet would ask its person")
        if self.spent_cents + cents > self.cap_cents:
            raise Refused(f"{self.spent_cents} + {cents} cents is over the run's cap of {self.cap_cents}")

    def pay(self, cents: int) -> None:
        self.allow(cents)
        self.spent_cents += cents


# ── chain reads (read-only JSON-RPC) ────────────────────────────────────────────

class Chain:
    def __init__(self, rpc: str = DEFAULT_RPC):
        self.rpc = rpc

    def call(self, method: str, params: list) -> Any:
        req = urllib.request.Request(
            self.rpc, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
            headers={"content-type": "application/json", "user-agent": "emporia-money-run"})
        with urllib.request.urlopen(req, timeout=30) as resp:   # noqa: S310 - a fixed https RPC
            out = json.load(resp)
        if "error" in out:
            raise StepFailed(f"rpc {method}: {out['error']}")
        return out["result"]

    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def block(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def usdc(self, address: str) -> int:
        data = "0x70a08231" + address.lower().removeprefix("0x").rjust(64, "0")
        return int(self.call("eth_call", [{"to": USDC, "data": data}, "latest"]), 16)

    def transfers(self, payer: str, payee: str, since_block: int) -> list[dict[str, Any]]:
        pad = lambda a: "0x" + a.lower().removeprefix("0x").rjust(64, "0")   # noqa: E731
        logs = self.call("eth_getLogs", [{"address": USDC, "fromBlock": hex(max(since_block - 2, 0)), "toBlock": "latest",
                                          "topics": [TRANSFER_TOPIC, pad(payer), pad(payee)]}])
        return [{"tx": g["transactionHash"], "block": int(g["blockNumber"], 16), "atomic": int(g["data"], 16)}
                for g in logs]

    def succeeded(self, tx: str) -> bool:
        rc = self.call("eth_getTransactionReceipt", [tx])
        return bool(rc) and rc.get("status") == "0x1"


def usd(atomic: int) -> str:
    return f"{atomic / 1_000_000:.6f}"


# ── agents: one Hermes-launched worker per profile ──────────────────────────────

class Agent:
    """A profile's Emporia tools, called as the agent calls them."""

    def __init__(self, name: str, home: Path, hermes: str, relay_url: str, logdir: Path):
        self.name, self.home, self.hermes, self.relay_url, self.logdir = name, home, hermes, relay_url, logdir
        self.proc: subprocess.Popen | None = None

    def _start(self) -> subprocess.Popen:
        from e2e import LAUNCH, WORKER      # the same launch the end-to-end run uses
        root = Path(os.path.realpath(self.hermes)).parent.parent
        sites = sorted(root.glob(".venv/lib/python3*/site-packages"))
        python = root / ".venv" / "bin" / "python"
        if not (python.is_file() and sites):
            raise StepFailed(f"no Hermes interpreter under {root}")
        env = {k: v for k, v in os.environ.items() if k != "EMPORIA_RELAY_URL"}
        env.update(HERMES_HOME=str(self.home), PYTHONPATH=os.pathsep.join([str(root), str(sites[-1])]))
        self.logdir.mkdir(parents=True, exist_ok=True)
        err = (self.logdir / f"worker-{self.name}.log").open("ab")
        proc = subprocess.Popen([str(python), "-c", LAUNCH, WORKER], env=env, text=True, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=err, bufsize=1)
        if proc.stdout.readline().strip() != '{"ready": true}':
            proc.kill()
            raise StepFailed(f"{self.name}'s tools did not start; see {self.logdir / ('worker-' + self.name + '.log')}")
        return proc

    def tool(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self.proc is None or self.proc.poll() is not None:
            self.proc = self._start()
        self.proc.stdin.write(json.dumps({"tool": name, "args": list(args), "kwargs": kwargs}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise StepFailed(f"{self.name}'s tools stopped")
        res = json.loads(line)
        if "raised" in res:
            raise StepFailed(f"{self.name}.{name} raised {res['raised']}")
        return res["ok"]

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self.proc.kill()          # our own child, by its handle


# ── the run ─────────────────────────────────────────────────────────────────────

@dataclass
class Report:
    steps: list[tuple[bool, str]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    def step(self, ok: bool, text: str) -> None:
        self.steps.append((ok, text))
        print(("✓ " if ok else "✗ ") + text, flush=True)

    def note(self, text: str) -> None:
        self.lines.append(text)
        print("  " + text, flush=True)


def link_tx(tx: str) -> str:
    return f"{EXPLORER}/tx/{tx}"


def link_addr(addr: str) -> str:
    return f"{EXPLORER}/address/{addr}"


def requirement_from(result: dict[str, Any]) -> dict[str, Any]:
    """The Base Sepolia `accepts` entry of a payment_required answer, or Refused if there is none."""
    accepts = (((result.get("detail") or {}).get("x402") or {}).get("accepts")) or []
    for req in accepts:
        if req.get("network") == NETWORK:
            return req
    raise Refused(f"the relay offers no x402 payment on {NETWORK}: {[a.get('network') for a in accepts]}")


def run(alpha: Any, beta: Any, chain: Any, report: Report, *, dry_run: bool, room_cents: int, service_cents: int,
        spend: Spend, relay_url: str, sleep: Callable[[float], None] = time.sleep, stamp: str = "") -> bool:
    stamp = stamp or datetime.now(UTC).strftime("%Y-%m-%d %H:%MZ")
    cleanup: list[Callable[[], None]] = []
    t0 = time.monotonic()
    try:
        # planned total, before anything happens
        for cents in (room_cents, service_cents):
            spend.allow(cents)
        if room_cents + service_cents > spend.cap_cents:
            raise Refused(f"the planned {room_cents + service_cents} cents is over the cap of {spend.cap_cents}")

        # who is who, and the starting balances
        wa, wb = alpha.tool("x402_wallet_info"), beta.tool("x402_wallet_info")
        a_addr, b_addr = wa.get("address"), wb.get("address")
        report.step(bool(a_addr and b_addr), f"wallets: alpha {a_addr}, beta {b_addr}")
        if not (a_addr and b_addr):
            return False
        if NETWORK not in (wa.get("pay_networks") or []) or NETWORK not in (wb.get("pay_networks") or []):
            raise Refused(f"an agent's wallet does not pay on {NETWORK}")
        report.step(chain.chain_id() == 84532, "the RPC is Base Sepolia (chain id 84532)")
        if chain.chain_id() != 84532:
            raise Refused("the RPC is not Base Sepolia")
        a0, b0 = chain.usdc(a_addr), chain.usdc(b_addr)
        report.note(f"balances before: alpha {usd(a0)} USDC, beta {usd(b0)} USDC")
        if b0 < (room_cents + service_cents) * ATOMIC_PER_CENT:
            raise Refused("beta does not hold enough test USDC for the run")
        if wa.get("registered_on_relay") != a_addr:
            raise Refused("alpha's wallet is not registered on the relay as where it is paid")

        # 1. paid room entry
        room = alpha.tool("create_room", f"money run {stamp}", room_type="private", gate_type="paid",
                          entry_fee_cents=room_cents, currency="USD",
                          description="a testnet money run: paid entry over x402 on Base Sepolia")
        room_id = (room.get("room") or {}).get("room_id") or (room.get("room") or {}).get("id")
        report.step(bool(room.get("success") and room_id), f"alpha made the paid room {room_id} ({room_cents} cents, x402)")
        if not room_id:
            raise StepFailed(f"create_room: {json.dumps(room)[:400]}")
        ask = beta.tool("join_room", room_id)
        report.step(ask.get("error") == "payment_required", "beta's join answers payment_required")
        req = requirement_from(ask)
        check_requirement(req, room_cents, a_addr)
        report.step(True, f"the requirement: {req['amount']} atomic USDC on {req['network']}, asset {req['asset']}, payTo alpha")
        if dry_run:
            report.note("dry run: not paying")
        else:
            spend.allow(room_cents)
            block = chain.block()
            joined = beta.tool("join_room", room_id, pay_with_x402=True, pay_network=NETWORK)
            cleanup.append(lambda: beta.tool("leave_room", room_id))   # the creator cannot leave or close a room
            report.step(bool(joined.get("success")), f"beta joined paying with x402: {json.dumps(joined)[:300] if not joined.get('success') else 'success'}")
            if not joined.get("success"):
                raise StepFailed("the room payment failed: stopping, nothing is retried")
            spend.pay(room_cents)
            member = beta.tool("get_room_messages", room_id)
            report.step(not (isinstance(member, dict) and member.get("error")), "beta is a member (it reads the room)")
            tx = _find_transfer(chain, b_addr, a_addr, block, room_cents * ATOMIC_PER_CENT, sleep)
            report.step(bool(tx), "on chain: beta -> alpha, exactly " + usd(room_cents * ATOMIC_PER_CENT) + " USDC"
                        + (f", {tx}" if tx else ""))
            if tx:
                report.note(link_tx(tx))
                report.step(chain.succeeded(tx), "the transaction succeeded")
            else:
                raise StepFailed("no matching USDC transfer found on chain")

        # 2. service purchase (the buyer makes the session)
        price = f"{service_cents / 100:.2f}"
        sess = beta.tool("create_session", "emporia:service:v1", config={
            "description": f"money run {stamp}: a testnet service", "requirements": ["reply with any text"],
            "deadline_hours": 24}, payment_mode="stripe_link", price=price, currency="USD")
        sid = sess.get("session_id")
        report.step(bool(sess.get("success") and sid), f"beta opened the service session {sid} ({service_cents} cents)"
                    + ("" if sess.get("success") else f": {json.dumps(sess)[:300]}"))
        if not sid:
            return False
        cleanup.append(lambda: beta.tool("abandon_session", sid))
        j = alpha.tool("join_session", sid)
        report.step(bool(j.get("success")), "alpha joined as the seller"
                    + ("" if j.get("success") else f": {json.dumps(j)[:300]}"))
        if not j.get("success"):
            return False
        if dry_run:
            fund = beta.tool("fund_session", sid)
            report.note("the funding requirement without paying: " + json.dumps(fund)[:500])
            report.step(True, "dry run: not funding")
        else:
            spend.allow(service_cents)
            block = chain.block()
            fund = beta.tool("fund_session", sid, pay_with_x402=True)
            if not fund.get("success"):
                report.step(False, "beta funds the purchase with x402: " + json.dumps(fund)[:600])
                report.note("stopping the purchase here; nothing was paid for it")
                return False
            spend.pay(service_cents)
            report.step(True, "beta funded the purchase")
            d = alpha.tool("submit_action", sid, "deliver", {"deliverable": f"money run {stamp}: delivered"},
                           "delivering the testnet service as agreed")
            report.step(bool(d.get("success")), "alpha delivered")
            c = beta.tool("confirm_delivery", sid, rationale="confirming the testnet delivery, it is as agreed")
            report.step(bool(c.get("success")), "beta confirmed delivery")
            st = beta.tool("get_settlements", sid)
            report.step("error" not in st, "settlement record: " + json.dumps(st)[:400])
            tx = _find_transfer(chain, b_addr, a_addr, block, 0, sleep, minimum=service_cents * ATOMIC_PER_CENT // 2)
            report.step(bool(tx), "on chain: the purchase's USDC reached alpha" + (f", {tx}" if tx else ""))
            if tx:
                report.note(link_tx(tx))
    except Refused as e:
        report.step(False, f"refused: {e}")
    except StepFailed as e:
        report.step(False, str(e))
    finally:
        for fn in reversed(cleanup):
            try:
                res = fn()
                if isinstance(res, dict):
                    report.note("cleanup: " + ("done" if res.get("success") else json.dumps(res)[:200]))
            except Exception as e:  # noqa: BLE001 - cleanup is best effort, and said so
                report.note(f"cleanup: {e}")
        try:
            if "a_addr" in locals() and a_addr:
                a1, b1 = chain.usdc(a_addr), chain.usdc(b_addr)
                report.note(f"balances after: alpha {usd(a1)} USDC ({(a1 - a0) / 1e6:+.6f}), "
                            f"beta {usd(b1)} USDC ({(b1 - b0) / 1e6:+.6f})")
        except Exception as e:  # noqa: BLE001
            report.note(f"balances after: unreadable ({e})")
        report.note(f"spent by this run: {spend.spent_cents} cents; elapsed {time.monotonic() - t0:.1f}s"
                    + ("; DRY RUN" if dry_run else ""))
    return all(ok for ok, _ in report.steps)


def _find_transfer(chain: Any, payer: str, payee: str, since: int, exact: int, sleep: Callable[[float], None],
                   minimum: int = 0) -> str:
    for _ in range(12):
        for t in chain.transfers(payer, payee, since):
            if (exact and t["atomic"] == exact) or (not exact and t["atomic"] >= minimum):
                return t["tx"]
        sleep(5)
    return ""


def profile_home(name: str, override: str) -> Path:
    if override:
        return Path(override)
    root = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return root / "profiles" / name


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="everything up to, not including, any payment")
    ap.add_argument("--alpha", default="alpha")
    ap.add_argument("--beta", default="beta")
    ap.add_argument("--alpha-home", default="")
    ap.add_argument("--beta-home", default="")
    ap.add_argument("--relay-url", default="http://localhost:8088")
    ap.add_argument("--rpc", default=DEFAULT_RPC, help="a Base Sepolia JSON-RPC endpoint (read-only use)")
    ap.add_argument("--hermes", default=shutil.which("hermes") or "")
    ap.add_argument("--room-cents", type=int, default=5)
    ap.add_argument("--service-cents", type=int, default=10)
    ap.add_argument("--cap-cents", type=int, default=SPEND_CAP_CENTS)
    ap.add_argument("--logdir", default="", help="where the workers' logs go (default: a folder beside this run's cwd)")
    args = ap.parse_args(argv)
    if not args.hermes:
        print("hermes is not on PATH (use --hermes)")
        return 2
    logdir = Path(args.logdir or "money-run-logs")
    alpha = Agent(args.alpha, profile_home(args.alpha, args.alpha_home), args.hermes, args.relay_url, logdir)
    beta = Agent(args.beta, profile_home(args.beta, args.beta_home), args.hermes, args.relay_url, logdir)
    report = Report()
    print(f"money run {'(dry run) ' if args.dry_run else ''}on {args.relay_url}, {NETWORK}")
    try:
        ok = run(alpha, beta, Chain(args.rpc), report, dry_run=args.dry_run, room_cents=args.room_cents,
                 service_cents=args.service_cents, spend=Spend(cap_cents=min(args.cap_cents, SPEND_CAP_CENTS)),
                 relay_url=args.relay_url)
    finally:
        alpha.close()
        beta.close()
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
