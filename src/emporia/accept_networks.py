"""`emporia wallet accept NAME`: which networks an agent takes payment on, and the setup question for it.

An agent's accept list is a list of CAIP-2 ids it signs with its own key and its relay stores
(`POST /agents/{id}/accept-networks`, shown on `GET /agents/{id}` as `accepts_networks`). A payment
request to the agent then offers only the networks the relay has configured AND the agent lists AND
the agent has a receive address for. Unset: every relay network it has a receive address for.

It is a person's setting, like the spending limits: refused while an unattended turn is live, held
from an agent's own terminal by the key guard, and no MCP tool changes it (`my_settings` shows it).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from emporia import networks as N

_FAMILY_NAME = {"evm": "EVM", "svm": "Solana"}


def _cli():
    from emporia import cli
    return cli


def payable_networks(has: dict[str, bool], mainnet: bool = False) -> list[str]:
    """The networks an agent could list: ones Emporia takes payment on, whose family it has an address for."""
    return [k for k, v in N.pay_networks().items() if has.get(v.family) and (v.testnet or mainnet)]


def default_list(has: dict[str, bool]) -> list[str]:
    """The recommended list: every testnet the agent has an address for."""
    return payable_networks(has, mainnet=False)


def option_lines(has: dict[str, bool]) -> list[tuple[str, str]]:
    """(caip2, one line) for each testnet it could list: what it is and where test funds come from."""
    out = []
    for n in default_list(has):
        net = N.NETWORKS[n]
        coin = "pathUSD" if n == "eip155:42431" else "USDC"
        funds = f"; test funds: {net.faucet}" if net.faucet else ""
        out.append((n, f"{N.label(n)} — {coin} to its {_FAMILY_NAME[net.family]} address{funds}"))
    return out


def names(ids: list[str] | None) -> str:
    return ", ".join(N.label(n) for n in ids) if ids else "every network the relay offers that it has an address for"


def changed_list(current: list[str] | None, base: list[str], networks: list[str] | None, add: list[str],
                 remove: list[str]) -> list[str]:
    """The list after `--networks` (replace), `--add` and `--remove`; an unset list starts from `base`."""
    out = list(networks) if networks else list(current if current else base)
    for n in add:
        if n not in out:
            out.append(n)
    return [n for n in out if n not in remove]


def read_state(profile: Path) -> dict[str, Any] | None:
    """The agent's accept list and registered receive families, from its relay; None when unreadable."""
    cli = _cli()
    _, agent_id, _ = cli._agent(profile.name)
    r = cli._as_agent(profile, (
        "import json\nfrom emporia import mcp_server as m\n"
        f"print(json.dumps(m.read_accept_networks(agent_id={agent_id!r})))\n"))
    try:
        d = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return d if d.get("success") else None


def publish(profile: Path, agent_id: str, networks: list[str]) -> tuple[bool, str]:
    """Sign and send the list as the agent ([] clears it). (ok, reason)."""
    cli = _cli()
    r = cli._as_agent(profile, (
        "import json\nfrom emporia import mcp_server as m\n"
        f"print(json.dumps(m.publish_accept_networks({networks!r}, agent_id={agent_id!r})))\n"))
    try:
        d = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, (r.stderr or r.stdout).strip()[-300:] or "no answer"
    if d.get("success"):
        return True, ""
    return False, str(d.get("detail") or d.get("error") or "refused")[:300]


def cmd_accept(args: Any, profile: Path, agent_id: str, dotenv: dict[str, str]) -> int:
    """`emporia wallet accept NAME [--networks a,b | --add x | --remove x | --clear]`."""
    cli = _cli()
    name = args.name
    unlocked = N.mainnet_unlocked(dotenv)
    changing = bool(args.networks or args.add or args.remove or args.clear)
    state = None if not cli.health() else read_state(profile)
    current = (state or {}).get("accepts_networks")
    if not changing:
        if state is None:
            print(f"wallet accept: the relay is not answering, so {name}'s list cannot be read "
                  "(emporia relay start, then try again)")
            return 1
        print(f"{name} takes payment on: {names(current)}" + ("" if current else "  (it never chose)"))
        print(f"  change it: emporia wallet accept {name} --networks base-sepolia,solana-devnet "
              f"(or --add NETWORK, --remove NETWORK, --clear)")
        return 0

    from emporia import autonomy
    if why := autonomy.unattended_live(profile / "plugin-data" / "emporia"):
        print(f"wallet accept: a {why} turn is running for {name}: where it takes payment is a person's to "
              "change, not something an unattended turn does. Run it yourself, when no turn is live.")
        return 1
    if state is None:
        print(f"wallet accept: the relay is not answering, or {name} is not registered on it "
              f"(emporia relay start; emporia wallet register {name})")
        return 1

    wanted = N.resolve_list(args.networks) if args.networks else []
    add, remove = N.resolve_list(args.add), N.resolve_list(args.remove)
    mentioned = [*wanted, *add]
    unknown = [n for n in [*mentioned, *remove] if N.family(n) is None]
    if unknown:
        print(f"wallet accept: unknown network(s) {', '.join(unknown)} — use a CAIP-2 id or one of "
              + ", ".join(sorted(N.ALIASES)))
        return 1
    read_only = [n for n in mentioned if n in N.NETWORKS and not N.NETWORKS[n].payments]
    if read_only:
        print(f"wallet accept: {', '.join(N.label(n) for n in read_only)} is read-only: Emporia takes no "
              "payment on it")
        return 1
    if cli._locked_out(mentioned, unlocked, name):
        return 1

    pay_to = state.get("x402_pay_to") or {}
    has = {f: bool(pay_to.get(f)) for f in ("evm", "svm")}
    if args.clear:
        final: list[str] = []
    else:
        final = changed_list(current, default_list(has), wanted, add, remove)
        if not final:
            print("wallet accept: that leaves no network; list at least one, or --clear to take every relay "
                  "network it has an address for")
            return 1
    for n in final:
        fam = N.family(n)
        if fam and not pay_to.get(fam):
            print(f"  ! no {_FAMILY_NAME[fam]} receive address registered yet for {N.label(n)}: nobody can pay "
                  f"{name} there until it has one — emporia wallet register {name}"
                  f"{' --solana-address ADDRESS' if fam == 'svm' else ' --address ADDRESS'} "
                  f"(or emporia wallet create {name}{' --solana' if fam == 'svm' else ''}, then register it)")
    ok, why = publish(profile, agent_id, final)
    if not ok:
        print(f"wallet accept: the relay did not take it: {why}")
        return 1
    print(f"{name} takes payment on: {names(final or None)}")
    return 0
