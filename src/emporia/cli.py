"""emporia — set up, check and run Emporia from one command (P127).

    emporia doctor                     what is set up, what is not, and how to fix it
    emporia relay start|stop|status|logs
    emporia login [--profile NAME]     Nous sign-in for that Hermes profile, verified
    emporia setup agent NAME           a slim agent profile (model + its key + Emporia)
    emporia setup relay                this machine's relay: x402/escrow preset, operator key
    emporia setup dashboard            the Emporia tab in the Hermes dashboard
    emporia wallet show|create|register|limits NAME
                                       an agent's wallets: addresses, networks, balances,
                                       spending limits; create keys only when asked

Everything here was a manual step at some point. The commands reuse the
installer's own functions (installer/install.py), so there is one way each
thing is done. `doctor` only reads; every command that writes says what it will
change first, never prints a secret, and ends with the restart it needs.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
_RELAY_ONLY = ("STRIPE_SECRET_KEY", "STRIPE_PROFILE_ID", "STRIPE_API_VERSION",
               "STRIPE_WEBHOOK_SECRET", "EMPORIA_MPP_TEMPO_ENABLED")
_TTY = sys.stdin.isatty() and sys.stdout.isatty()


# ─── shared ──────────────────────────────────────────────────────────────────

_installer_module = None


def installer():
    """installer/install.py, loaded once (it is a script, not a package)."""
    global _installer_module
    if _installer_module is None:
        spec = importlib.util.spec_from_file_location("emporia_installer",
                                                      REPO / "installer" / "install.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["emporia_installer"] = mod
        spec.loader.exec_module(mod)
        _installer_module = mod
    return _installer_module


def this_profile() -> Path | None:
    """The Hermes profile holding this checkout: .../profiles/<name>/emporia."""
    for parent in REPO.parents:
        if (parent / "config.yaml").exists():
            return parent
    return None


def profiles_root() -> Path:
    p = this_profile()
    return p.parent if p else Path.home()


def relay_url() -> str:
    return os.getenv("EMPORIA_RELAY_URL", "http://localhost:8088").rstrip("/")


def hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")


def ask(question: str, default: bool) -> bool:
    if not _TTY:
        return default
    hint = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {hint} ").strip().lower()
    return default if not answer else answer.startswith("y")


class Report:
    """✓ / ! / ✗ lines with a fix under each problem; exit status 1 on any ✗."""

    def __init__(self) -> None:
        self.failed = False

    def section(self, title: str) -> None:
        print(f"\n{title}")

    def ok(self, msg: str) -> None:
        print(f"  ✓ {msg}")

    def warn(self, msg: str, fix: str = "") -> None:
        print(f"  ! {msg}" + (f"\n      fix: {fix}" if fix else ""))

    def fail(self, msg: str, fix: str = "") -> None:
        self.failed = True
        print(f"  ✗ {msg}" + (f"\n      fix: {fix}" if fix else ""))


def health(timeout: float = 3.0) -> dict | None:
    import httpx
    try:
        r = httpx.get(f"{relay_url()}/health", timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


# ─── relay ───────────────────────────────────────────────────────────────────

def _relay_files() -> tuple[Path, Path]:
    home = (this_profile() or REPO) / "home" / ".hermes"
    return home / "emporia-relay.pid", REPO / ".relay.log"


def cmd_relay(args: argparse.Namespace) -> int:
    pid_file, log = _relay_files()
    if args.action == "status":
        h = health()
        if not h:
            print(f"relay: not answering at {relay_url()}")
            return 1
        print(f"relay: up at {relay_url()} — v{h.get('version')}, Stripe {h.get('stripe_mode') or 'off'}, "
              f"x402 {'on ' + ','.join(h.get('x402_networks') or []) if h.get('x402_enabled') else 'off'}, "
              f"fee {h.get('operator_fee_bps', 0) / 100:.1f}%")
        return 0
    if args.action == "logs":
        lines = log.read_text(errors="replace").splitlines() if log.exists() else []
        print("\n".join(lines[-args.lines:]) or f"(no log at {log})")
        return 0
    if args.action == "stop":
        try:
            pid = int(pid_file.read_text())
            os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            print("relay: no relay started by `emporia relay start` is running"
                  + ("" if not health() else " (one is answering — stop it where it was started)"))
            return 1
        pid_file.unlink(missing_ok=True)
        print(f"relay: stopped (pid {pid})")
        return 0
    # start
    if health():
        print(f"relay: already up at {relay_url()}")
        return 0
    exe = Path(sys.executable).with_name("emporia-relay")
    argv = [str(exe)] if exe.exists() else [sys.executable, "-m", "emporia.relay_server"]
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as out:
        proc = subprocess.Popen(argv, cwd=REPO, stdout=out, stderr=out,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    pid_file.write_text(str(proc.pid))
    for _ in range(80):
        if health(timeout=1):
            print(f"relay: started at {relay_url()} (pid {proc.pid}, log {log})")
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    print(f"relay: did not start — see `emporia relay logs` ({log})")
    return 1


# ─── doctor ──────────────────────────────────────────────────────────────────

def _agent_profiles(root: Path) -> list[tuple[Path, dict]]:
    """Hermes profiles wired to Emporia: (dir, its mcp_servers.emporia entry)."""
    inst = installer()
    out = []
    for cfg in sorted(root.glob("*/config.yaml")):
        entry = (inst.load_yaml(cfg).get("mcp_servers") or {}).get("emporia")
        if isinstance(entry, dict):
            out.append((cfg.parent, entry))
    return out


def _signed_get(agent_id: str, key_dir: Path, path: str) -> tuple[int, Any]:
    """GET `path` on the relay as `agent_id`, signed with its own key."""
    from emporia.console.agents import LocalAgent
    from emporia.console.relay import RelayClient

    async def go():
        client = RelayClient(relay_url(), timeout=10)
        try:
            r = await client.forward(LocalAgent(agent_id, key_dir), "GET", path, "", {}, b"")
            return r.status_code, (r.json() if r.content else None)
        finally:
            await client.close()
    try:
        return asyncio.run(go())
    except Exception as e:  # noqa: BLE001 — reported, not raised
        return 0, str(e)


def _relay_code_behind(h: dict) -> str:
    """P139: why the running relay is older than this checkout, or "". Code
    changes need a relay restart, and nothing said when one was owed."""
    from emporia.schema import SCHEMA_VERSION
    running = h.get("schema_version")
    if running is None:
        return "it reports no schema version"
    if running < SCHEMA_VERSION:
        return f"schema {running}, checkout {SCHEMA_VERSION}"
    from emporia.module_sdk import MODULE_REGISTRY, load_bundled_modules
    load_bundled_modules()
    missing = sorted(t for t in MODULE_REGISTRY
                     if t.startswith("emporia:") and t not in (h.get("modules") or []))
    return f"missing games: {', '.join(missing)}" if missing else ""


def doctor_relay(rep: Report) -> dict | None:
    rep.section(f"Relay  {relay_url()}")
    h = health()
    if not h:
        rep.fail("not answering", "emporia relay start   (or check EMPORIA_RELAY_URL)")
        return None
    rep.ok(f"answering — v{h.get('version')}, fee {h.get('operator_fee_bps', 0) / 100:.1f}%")
    stale = _relay_code_behind(h)
    if stale:
        rep.fail(f"the relay runs older code than this checkout ({stale})",
                 "emporia relay stop && emporia relay start")
    mode = h.get("stripe_mode")
    if not h.get("stripe_enabled"):
        rep.warn("Stripe off", "set STRIPE_SECRET_KEY (an sk_test_ key for testing) in the relay's .env")
    elif mode == "live":
        rep.warn("Stripe is in LIVE mode", "use an sk_test_ key unless this relay is in production")
    else:
        rep.ok(f"Stripe {mode} mode" + ("" if h.get("stripe_profile_ready")
                                         else " (no MPP profile — SPT/MPP payments unavailable)"))
    from emporia.networks import label
    if h.get("x402_enabled"):
        rep.ok(f"USDC (x402) on {', '.join(label(n) for n in h.get('x402_networks') or [])}")
    else:
        rep.warn("x402 off", "emporia setup relay --preset base-sepolia")
    if os.getenv("EMPORIA_ESCROW_NETWORK"):
        try:
            from emporia import escrow_chain
            from emporia.operator_signer import load_operator_signer
            cfg = escrow_chain.load_config()
            signer = load_operator_signer(cfg.network)
            rep.ok(f"escrow on {cfg.network}, operator {signer.address[:10]}…")
        except Exception as e:  # noqa: BLE001
            rep.fail(f"escrow configured but not usable: {str(e)[:120]}",
                     "emporia setup relay --preset base-sepolia")
    else:
        rep.warn("escrow off — x402 service purchases unavailable", "emporia setup relay --preset base-sepolia")
    if h.get("tempo_network"):
        rep.ok(f"Tempo on {label(h['tempo_network'])}")
    return h


def doctor_agent(rep: Report, profile: Path, entry: dict, relay_up: bool) -> None:
    inst = installer()
    name = profile.name
    env = entry.get("env") or {}
    agent_id = env.get("EMPORIA_AGENT_ID") or name
    rep.section(f"Agent  {agent_id}  ({profile})")
    missing = [k for k in ("EMPORIA_DOTENV", "EMPORIA_KEYS_DIR", "EMPORIA_HERMES_BIN") if not env.get(k)]
    if missing:
        rep.fail(f"MCP entry lacks {', '.join(missing)} — it may use another profile's keys or token",
                 f"emporia setup agent {name}")
    else:
        rep.ok("MCP entry names this profile's keys, .env and Hermes")
    key_dir = Path(env.get("EMPORIA_KEYS_DIR") or profile / "home" / ".hermes" / "keys")
    from emporia.identity import _keyfile_stem
    if not (key_dir / f"{_keyfile_stem(agent_id)}.priv").exists():
        rep.fail("no identity key", "it is created when the agent's MCP server first starts")
        return
    dotenv = inst.harvest_env(profile)
    cfg_env = inst.load_yaml(profile / "config.yaml").get("env") or {}
    leaked = [k for k in _RELAY_ONLY if k in dotenv or k in cfg_env]
    if leaked and profile != this_profile():
        rep.fail(f"holds the relay's settings: {', '.join(leaked)} (an agent never needs them)",
                 f"emporia setup agent {name}   (strips them)")
    model = inst.load_yaml(profile / "config.yaml").get("model") or {}
    key_name = inst._PROVIDER_KEYS.get(str(model.get("provider") or "").lower())
    if key_name and not (dotenv.get(key_name) or os.environ.get(key_name)):
        rep.fail(f"model {model.get('default')} needs {key_name} in the profile .env",
                 f"emporia setup agent {name}")
    elif model:
        rep.ok(f"model {model.get('default') or model.get('provider')}")
    from emporia import nous_token
    if nous_token.is_current(nous_token.from_hermes(profile) or ""):
        rep.ok("Nous login current (via Hermes)")
    else:
        rep.warn("no current Nous login for this profile — registers key_only, can't enter bounties",
                 f"emporia login --profile {name}")
    from emporia.networks import family, label, resolve_list
    wallet = key_dir / f"{_keyfile_stem(agent_id)}.x402.secp256k1"
    sol_wallet = key_dir / f"{_keyfile_stem(agent_id)}.x402.ed25519"
    pay_nets = resolve_list(dotenv.get("EMPORIA_X402_PAY_NETWORKS", ""))
    no_key = [n for n in pay_nets
              if not (sol_wallet if family(n) == "svm" else wallet).exists()]
    if not wallet.exists() and not sol_wallet.exists():
        rep.warn("no wallet — can pay by card only", f"emporia wallet create {name}")
    elif not pay_nets:
        rep.fail("a wallet but no EMPORIA_X402_PAY_NETWORKS — it will pay on no network",
                 f"emporia wallet limits {name} --networks base-sepolia")
    elif no_key:
        rep.fail(f"allowed to pay on {', '.join(label(n) for n in no_key)} with no wallet for it",
                 f"emporia wallet create {name}" + (" --solana" if any(family(n) == "svm" for n in no_key) else ""))
    else:
        rep.ok(f"wallet{'s' if wallet.exists() and sol_wallet.exists() else ''}, pays on "
               f"{', '.join(label(n) for n in pay_nets)}, "
               f"≤ ${int(dotenv.get('EMPORIA_X402_MAX_PAYMENT_CENTS', 1000)) / 100:.2f} each, "
               f"≤ ${int(dotenv.get('EMPORIA_X402_MAX_DAILY_CENTS', 5000)) / 100:.2f}/day")
    plugin = profile / "plugins" / "platforms" / "emporia"
    repo_digest = inst._tree_digest(REPO / "hermes_plugin" / "emporia")
    if not plugin.exists():
        rep.warn("no Emporia platform plugin — relay events (DMs, your turn) won't reach the agent",
                 f"emporia setup agent {name}")
    elif inst._tree_digest(plugin) != repo_digest:
        rep.warn("platform plugin is out of date",
                 f"emporia setup agent {name}, then `hermes gateway restart`")
    else:
        rep.ok("platform plugin current")
    if relay_up:
        status, profile_json = _signed_get(agent_id, key_dir, f"agents/{agent_id}")
        if status == 200 and isinstance(profile_json, dict):
            pay_to = (profile_json.get("x402_pay_to") or {}).get("evm")
            # P128: a relay with Stripe on can still have no way to pay this agent.
            if (health() or {}).get("stripe_enabled") and \
                    profile_json.get("stripe_account_status", "none") != "active":
                rep.warn("cannot receive Stripe payouts (no active Connected Account) — "
                         "wager winnings and card-paid fees to it stay owed",
                         "in the Stripe Dashboard (test mode) enable Accounts v1 support "
                         "(Developers → API policies) or complete Connect → Platform profile; "
                         f"then restart its gateway so {agent_id} re-registers")
            rep.ok(f"registered, {profile_json.get('trust_level')}"
                   + (f", paid at {pay_to[:10]}…" if pay_to else ""))
            # P150: without a published key, its DMs and private sessions
            # cannot be sealed to it — they fall back to plaintext.
            if not profile_json.get("encryption_public_key"):
                rep.warn("no encryption key published — DMs and private sessions with it stay "
                         "plaintext, readable by the relay",
                         f"restart its MCP (`hermes -p {name} gateway restart`); it publishes the key on registering")
            else:
                rep.ok("encryption key published — its DMs and private sessions are sealed")
            if wallet.exists() and not pay_to:
                rep.warn("EVM wallet not registered as where it is paid", f"emporia wallet register {name}")
            if sol_wallet.exists() and not (profile_json.get("x402_pay_to") or {}).get("svm"):
                rep.warn("Solana wallet not registered as where it is paid", f"emporia wallet register {name}")
        else:
            rep.warn("not registered on this relay yet",
                     f"start its gateway or MCP (`hermes -p {name} gateway restart`)")


def doctor_dashboard(rep: Report) -> None:
    inst = installer()
    home = hermes_home()
    rep.section(f"Dashboard tab  ({home / 'plugins' / 'emporia'})")
    dst = home / "plugins" / "emporia"
    if not dst.exists():
        rep.warn("not installed — the dashboard is only at the relay's /ui/", "emporia setup dashboard")
        return
    enabled = ((inst.load_yaml(home / "config.yaml").get("plugins") or {}).get("enabled") or [])
    if "emporia" not in enabled:
        rep.fail("installed but not enabled — Hermes will not run its backend",
                 "hermes plugins enable emporia")
    else:
        rep.ok("installed and enabled")
    built = REPO / "dashboard" / "dist-plugin"
    if built.exists() and inst._tree_digest(built) != inst._tree_digest(dst / "dashboard" / "dist"):
        rep.warn("installed bundle differs from this checkout's build",
                 "emporia setup dashboard, then restart the Hermes dashboard")


def cmd_doctor(args: argparse.Namespace) -> int:
    rep = Report()
    h = doctor_relay(rep)
    for profile, entry in _agent_profiles(Path(args.profiles_root) if args.profiles_root else profiles_root()):
        if args.profile and profile.name != args.profile:
            continue
        doctor_agent(rep, profile, entry, relay_up=h is not None)
    doctor_dashboard(rep)
    print("\n" + ("Problems found (✗) — fix them in order." if rep.failed
                  else "No problems found. Warnings (!) are optional."))
    return 1 if rep.failed else 0


# ─── login ───────────────────────────────────────────────────────────────────

def cmd_login(args: argparse.Namespace) -> int:
    """Nous sign-in for one Hermes profile — `hermes -p NAME auth add nous` —
    then confirm Hermes now hands that profile a current token (P114)."""
    from emporia import nous_token

    name = args.profile or (this_profile().name if this_profile() else "")
    profile = profiles_root() / name
    if not name or not (profile / "config.yaml").exists():
        print(f"login: no Hermes profile {name!r} under {profiles_root()}")
        return 1
    print(f"Signing {name} in to Nous through Hermes. If it offers to import existing "
          f"credentials, answer Y.")
    subprocess.run(["hermes", "-p", name, "auth", "add", "nous", "--type", "oauth"], check=False)
    if nous_token.is_current(nous_token.from_hermes(profile) or ""):
        print(f"login: {name} has a current Nous login — it registers as nous_verified.")
        return 0
    print(f"login: Hermes still has no current Nous login for {name}. Run it again and complete "
          f"the browser step, or `hermes -p {name} auth status nous` to see why.")
    return 1


# ─── setup ───────────────────────────────────────────────────────────────────

def _mcp_env(profile: Path) -> dict[str, str]:
    entry = (installer().load_yaml(profile / "config.yaml").get("mcp_servers") or {}).get("emporia") or {}
    return {k: str(v) for k, v in (entry.get("env") or {}).items() if k != "EMPORIA_NOUS_JWT"}


def _as_agent(profile: Path, code: str) -> subprocess.CompletedProcess:
    """Run `code` the way the agent's MCP server runs: its own env, nothing else."""
    env = {"PATH": os.defpath, "HOME": str(Path.home()), **_mcp_env(profile)}
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                          timeout=120)


def cmd_setup_agent(args: argparse.Namespace) -> int:
    inst = installer()
    me = this_profile()
    name = args.name
    profile = profiles_root() / name
    existing = (profile / "config.yaml").exists()
    print(f"{'Updating' if existing else 'Creating'} agent profile {name} — bare: the model, its one API "
          f"key, and Emporia (MCP, skill, platform plugin, own keys).")
    sync_model = args.sync_model or (existing and me is not None and me != profile and ask(
        f"Use the same model and API key as {me.name if me else 'this profile'}?", True))
    wallet = args.wallet or ask(f"Give {name} an EVM wallet, so it can pay and be paid in USDC on "
                                f"Base and in stablecoin on Tempo?", False)
    solana = args.solana or (wallet and ask(f"Also a Solana wallet, for USDC on Solana?", False))
    families = (["evm"] if wallet else []) + (["svm"] if solana else [])
    if args.pay_networks:
        from emporia.networks import resolve_list
        networks = ",".join(resolve_list(args.pay_networks))
    else:   # P160: what this relay takes, on the wallets it has
        networks = ",".join(_default_networks(families))
    wallet = wallet or solana
    inst.create_profile(
        name=name, relay_url=relay_url(), current_profile_dir=me, inherit_env=args.inherit_env,
        sync_model=bool(sync_model), x402_pay_networks=networks,
        x402_max_payment_cents=args.max_payment_cents if wallet else None,
        x402_max_daily_cents=args.max_daily_cents if wallet else None,
    )
    if wallet:
        from emporia.networks import NETWORKS, label
        _, agent_id, _ = _agent(name)
        for kind in families:
            address = _create_wallet(profile, agent_id, kind == "svm")
            print(f"  {'Solana' if kind == 'svm' else 'x402'} wallet: {address}")
        nets = [n for n in networks.split(",") if n]
        print(f"  Pays on {', '.join(label(n) for n in nets) or 'no network'}; at most "
              f"${args.max_payment_cents / 100:.2f} a payment and ${args.max_daily_cents / 100:.2f} a day.")
        for n in nets:
            if n in NETWORKS and NETWORKS[n].faucet:
                print(f"  Fund it on {label(n)}: {NETWORKS[n].faucet}")
    if args.register:
        if not health():
            print("  Not registered: the relay is not answering (emporia relay start, then re-run).")
        else:
            r = _as_agent(profile, (
                "from emporia import mcp_server as m\n"
                f"r = m.register_agent(agent_id={name!r})\n"
                "print('registered', r.get('trust_level') if r.get('success') else r.get('error'))\n"
                + "".join(f"w = m.register_x402_wallet(agent_id={name!r}, network_family={f!r})\n"
                          f"print({f!r} + ' wallet', 'registered' if w.get('success') else "
                          f"w.get('error') or w.get('detail'))\n" for f in families)))
            print("  " + (r.stdout.strip().replace("\n", "\n  ") or r.stderr.strip()[-300:]))
    inst.print_restart_instructions(list(inst._TOUCHED_PROFILES))
    return 0


_BASE_SEPOLIA = {
    "EMPORIA_X402_ENABLED": "1",
    "EMPORIA_X402_FACILITATOR_URL": "https://x402.org/facilitator",
    "EMPORIA_X402_NETWORKS": "eip155:84532",
    "EMPORIA_X402_ASSET_EIP155_84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "EMPORIA_X402_EXTRA_EIP155_84532": '{"name":"USDC","version":"2"}',
    "EMPORIA_X402_RPC_EIP155_84532": "https://sepolia.base.org",
    "EMPORIA_ESCROW_NETWORK": "eip155:84532",
    "EMPORIA_ESCROW_RPC_URL": "https://sepolia.base.org",
    "EMPORIA_ESCROW_CONTRACT": "0xf96815976523E00e65Be8f34cA5e64b4f41EB19c",
    "EMPORIA_ESCROW_COLLECTOR": "0x8612dfdc421f80336cd14E8EF9cb1E765dB5ab88",
    "EMPORIA_OPERATOR_SIGNER": "env",
}


def _extra_networks(names: list[str], facilitator: str) -> dict[str, str]:
    """P160: settings that add a testnet beside the base preset. Solana needs
    the facilitator's fee payer, read from its /supported list (public)."""
    import json
    import re

    import httpx

    from emporia.networks import NETWORKS, resolve
    updates: dict[str, str] = {}
    for name in names:
        net = resolve(name)
        if net == "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1":
            kinds = httpx.get(f"{facilitator.rstrip('/')}/supported", timeout=15).json().get("kinds") or []
            fee_payer = next(((k.get("extra") or {}).get("feePayer") for k in kinds
                              if k.get("network") == net and k.get("scheme") == "exact"), None)
            if not fee_payer:
                raise ValueError(f"the facilitator {facilitator} does not offer Solana devnet")
            suffix = re.sub(r"[:-]", "_", net.upper())
            updates[f"EMPORIA_X402_ASSET_{suffix}"] = NETWORKS[net].usdc
            updates[f"EMPORIA_X402_EXTRA_{suffix}"] = json.dumps({"feePayer": fee_payer})
            updates["EMPORIA_X402_NETWORKS"] = ",".join(
                [*_BASE_SEPOLIA["EMPORIA_X402_NETWORKS"].split(","), net])
        elif net == "eip155:42431":
            updates["EMPORIA_TEMPO_NETWORK"] = net
            updates["EMPORIA_TEMPO_TOKEN"] = NETWORKS[net].usdc
        else:
            raise ValueError(f"no testnet preset for {name!r} — choose solana-devnet or tempo-moderato")
    return updates


def merge_env_file(path: Path, updates: dict[str, str]) -> list[str]:
    """Set `updates` in a .env, keeping every other line as it was. Returns the
    keys whose value changed. A dated 0600 backup is kept beside it first."""
    lines = path.read_text().splitlines() if path.exists() else []
    changed, seen = [], set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in updates:
            seen.add(key)
            if line != f"{key}={updates[key]}":
                lines[i] = f"{key}={updates[key]}"
                changed.append(key)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}")
            changed.append(key)
    if changed:
        if path.exists():
            backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%dT%H%M%S')}")
            backup.write_bytes(path.read_bytes())
            backup.chmod(0o600)
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o600)
    return changed


def cmd_setup_relay(args: argparse.Namespace) -> int:
    """This machine's relay: the x402 + escrow preset and its operator key.
    Every value written is public (contract and token addresses, URLs, your fee
    address); the operator key goes to its own 0600 file, never the .env."""
    import re

    from emporia import operator_signer

    me = this_profile()
    if me is None:
        print("setup relay: run it from the relay profile's emporia checkout")
        return 1
    if args.preset != "base-sepolia":
        print("setup relay: the only preset so far is base-sepolia (testnet)")
        return 1
    updates = dict(_BASE_SEPOLIA)
    if args.with_networks:
        try:
            updates.update(_extra_networks([n for n in args.with_networks.split(",") if n.strip()],
                                           _BASE_SEPOLIA["EMPORIA_X402_FACILITATOR_URL"]))
        except Exception as e:  # noqa: BLE001
            print(f"setup relay: {e}")
            return 1
    fee = args.fee_receiver or os.getenv("EMPORIA_ESCROW_FEE_RECEIVER", "")
    if not fee and _TTY:
        fee = input("Address that receives the 2.5% fee (a wallet you control, ideally a Safe): ").strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", fee or ""):
        print("setup relay: a fee-receiver address (0x… 40 hex) is required — --fee-receiver")
        return 1
    updates["EMPORIA_ESCROW_FEE_RECEIVER"] = fee
    try:
        signer = operator_signer.load_operator_signer("eip155:84532")
        address, made = signer.address, False
    except Exception:  # noqa: BLE001 — no key yet
        address, made = None, True
    if made:
        if not ask("No operator key yet. Create one (it captures and voids escrow holds, and pays gas)?", True):
            return 1
        address = operator_signer.generate_key()
        print(f"  operator key created: {operator_signer.key_path()} (0600), address {address}")
    updates["EMPORIA_OPERATOR_ADDRESS"] = address
    env_path = me / ".env"
    print(f"Will set in {env_path}:")
    for k, v in updates.items():
        print(f"  {k}={v}")
    if not args.yes and not ask("Write these?", True):
        return 1
    changed = merge_env_file(env_path, updates)
    print(f"  {len(changed)} setting(s) changed" + (" (backup beside it)" if changed else ""))
    if os.getenv("STRIPE_SECRET_KEY", "").startswith(("sk_live_", "rk_live_")):
        print("  ! STRIPE_SECRET_KEY is a live key — use an sk_test_ key for testing.")
    print(f"  Fund the operator {address} with a little Base Sepolia ETH for gas.")
    print("  Restart the relay: emporia relay stop && emporia relay start")
    return 0


def cmd_setup_dashboard(args: argparse.Namespace) -> int:
    inst = installer()
    me = this_profile()
    if me is None:
        print("setup dashboard: run it from a profile's emporia checkout")
        return 1
    inst.install_dashboard_plugin(me, relay_url(), Path(args.hermes_home) if args.hermes_home
                                  else hermes_home())
    inst.print_restart_instructions([])
    return 0


# ─── wallet ──────────────────────────────────────────────────────────────────
# P160: an agent's wallets in one place. Keys are created only on request, never
# overwritten, mode 0600, and never printed; addresses and balances are public.

def _agent(name: str) -> tuple[Path, str, dict[str, str]]:
    profile = profiles_root() / name
    env = _mcp_env(profile) if (profile / "config.yaml").exists() else {}
    return profile, env.get("EMPORIA_AGENT_ID") or name, env


def _wallet_cmd(profile: Path, agent_id: str, *args: str) -> subprocess.CompletedProcess:
    """`python -m emporia.agent_wallet ...` as the agent: its own keys directory."""
    env = {"PATH": os.defpath, "HOME": str(Path.home()), **_mcp_env(profile)}
    return subprocess.run([sys.executable, "-m", "emporia.agent_wallet", *args, "--agent-id", agent_id],
                          env=env, capture_output=True, text=True, timeout=60)


def _wallet_address(profile: Path, agent_id: str, solana: bool) -> str | None:
    r = _wallet_cmd(profile, agent_id, "address", *(["--solana"] if solana else []))
    return r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else None


def _create_wallet(profile: Path, agent_id: str, solana: bool) -> str | None:
    """The address, creating the key if there is none. Never replaces a key."""
    existing = _wallet_address(profile, agent_id, solana)
    if existing:
        return existing
    r = _wallet_cmd(profile, agent_id, "generate", *(["--solana"] if solana else []))
    found = [ln.removeprefix("Address: ").strip() for ln in r.stdout.splitlines() if ln.startswith("Address: ")]
    if not found:
        print(f"  could not create the {'Solana' if solana else 'EVM'} wallet: {r.stderr.strip()[-200:]}")
    return found[0] if found else None


def _register_wallets(profile: Path, agent_id: str, families: list[str]) -> None:
    if not health():
        print("  Not registered: the relay is not answering (emporia relay start, then "
              f"`emporia wallet register {profile.name}`).")
        return
    code = "from emporia import mcp_server as m\n" + "".join(
        f"w = m.register_x402_wallet(agent_id={agent_id!r}, network_family={f!r})\n"
        f"print({f!r}, 'registered' if w.get('success') else (w.get('error') or w.get('detail')))\n"
        for f in families)
    r = _as_agent(profile, code)
    for line in (r.stdout.strip() or r.stderr.strip()[-300:]).splitlines():
        print(f"  {'EVM' if line.startswith('evm') else 'Solana' if line.startswith('svm') else ''} "
              f"{line.split(' ', 1)[-1]}")


def _default_networks(families: list[str]) -> list[str]:
    """What an agent pays on by default: the relay's x402 networks it has a
    wallet for, plus Tempo for an EVM wallet; Base Sepolia when the relay is
    not answering."""
    from emporia.networks import family
    h = health() or {}
    nets = [n for n in (h.get("x402_networks") or []) if family(n) in families]
    if "evm" in families and h.get("tempo_network"):
        nets.append(h["tempo_network"])
    return nets or (["eip155:84532"] if "evm" in families else [])


def cmd_wallet(args: argparse.Namespace) -> int:
    from emporia.networks import NETWORKS, family, label, resolve_list, stablecoin_balance

    profile, agent_id, env = _agent(args.name)
    if not env:
        print(f"wallet: {args.name} is not an Emporia agent profile under {profiles_root()} "
              f"(emporia setup agent {args.name})")
        return 1
    dotenv = installer().harvest_env(profile)

    if args.action == "create":
        kinds = (["evm"] if args.evm or not args.solana else []) + (["svm"] if args.solana else [])
        for kind in kinds:
            existed = _wallet_address(profile, agent_id, kind == "svm")
            address = _create_wallet(profile, agent_id, kind == "svm")
            name = "Solana" if kind == "svm" else "EVM"
            print(f"{name} wallet {'(already there)' if existed else 'created'}: {address}")
            for n in NETWORKS.values():
                if n.family == kind and n.testnet and n.faucet:
                    print(f"  fund on {label(n.caip2)}: {n.faucet}")
        if args.register:
            _register_wallets(profile, agent_id, kinds)
        print(f"Next: `emporia wallet limits {args.name} --networks ...` to choose where it may pay.")
        return 0

    if args.action == "register":
        kinds = [k for k, solana in (("evm", False), ("svm", True)) if _wallet_address(profile, agent_id, solana)]
        if not kinds:
            print(f"wallet: {args.name} has no wallet to register (emporia wallet create {args.name})")
            return 1
        _register_wallets(profile, agent_id, kinds)
        return 0

    if args.action == "limits":
        updates: dict[str, str] = {}
        if args.networks:
            nets = resolve_list(args.networks)
            unknown = [n for n in nets if family(n) is None]
            if unknown:
                print(f"wallet: unknown network(s) {', '.join(unknown)} — use a CAIP-2 id or one of "
                      f"base-sepolia, base, solana-devnet, solana, tempo-moderato, tempo")
                return 1
            for n in nets:
                if not _wallet_address(profile, agent_id, family(n) == "svm"):
                    print(f"  ! no {'Solana' if family(n) == 'svm' else 'EVM'} wallet yet for {label(n)} "
                          f"— emporia wallet create {args.name}{' --solana' if family(n) == 'svm' else ''}")
            updates["EMPORIA_X402_PAY_NETWORKS"] = ",".join(nets)
        if args.max_payment_cents is not None:
            updates["EMPORIA_X402_MAX_PAYMENT_CENTS"] = str(args.max_payment_cents)
        if args.max_daily_cents is not None:
            updates["EMPORIA_X402_MAX_DAILY_CENTS"] = str(args.max_daily_cents)
        if not updates:
            print("wallet limits: give --networks, --max-payment-cents and/or --max-daily-cents")
            return 1
        changed = merge_env_file(profile / ".env", updates)
        for k in updates:
            print(f"  {k}={updates[k]}{'' if k in changed else '  (unchanged)'}")
        if changed:
            installer().print_restart_instructions([profile])
        return 0

    # show
    print(f"Wallets for {agent_id}  ({profile})")
    allowed = resolve_list(dotenv.get("EMPORIA_X402_PAY_NETWORKS", ""))
    for kind, solana, name in (("evm", False, "EVM"), ("svm", True, "Solana")):
        address = _wallet_address(profile, agent_id, solana)
        if not address:
            print(f"  {name:7} none — emporia wallet create {args.name}{' --solana' if solana else ''}")
            continue
        print(f"  {name:7} {address}")
        for n in [n for n in allowed if family(n) == kind]:
            bal = stablecoin_balance(n, address) if not args.offline else None
            print(f"          pays on {label(n)}" + (f": {bal:,.2f} USDC" if bal is not None else ""))
    if not allowed:
        print(f"  pays on no network — emporia wallet limits {args.name} --networks base-sepolia")
    print(f"  limits  ${int(dotenv.get('EMPORIA_X402_MAX_PAYMENT_CENTS', 1000)) / 100:.2f} a payment, "
          f"${int(dotenv.get('EMPORIA_X402_MAX_DAILY_CENTS', 5000)) / 100:.2f} a day")
    h = health()
    if h:
        offered = [label(n) for n in h.get("x402_networks") or []]
        print(f"  relay   takes USDC on {', '.join(offered) or 'no network'}"
              + (f"; Tempo on {label(h['tempo_network'])}" if h.get("tempo_network") else ""))
    return 0


# ─── entry ───────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="emporia", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="check the relay, every agent profile and the dashboard tab")
    d.add_argument("--profile", default="", help="only this agent profile")
    d.add_argument("--profiles-root", default="")
    d.set_defaults(func=cmd_doctor)

    r = sub.add_parser("relay", help="start, stop or inspect this machine's relay")
    r.add_argument("action", choices=["start", "stop", "status", "logs"])
    r.add_argument("--lines", type=int, default=40)
    r.set_defaults(func=cmd_relay)

    lg = sub.add_parser("login", help="Nous sign-in for a Hermes profile, verified")
    lg.add_argument("--profile", default="", help="default: the profile holding this checkout")
    lg.set_defaults(func=cmd_login)

    st = sub.add_parser("setup", help="set up an agent, this relay, or the dashboard tab")
    ss = st.add_subparsers(dest="what", required=True)
    ag = ss.add_parser("agent", help="create or update a slim agent profile")
    ag.add_argument("name")
    ag.add_argument("--wallet", action="store_true", help="give it an EVM wallet (USDC on Base, Tempo)")
    ag.add_argument("--solana", action="store_true", help="give it a Solana wallet (USDC on Solana)")
    ag.add_argument("--pay-networks", default="",
                    help="where it may pay, e.g. base-sepolia,solana-devnet (default: what the relay takes)")
    ag.add_argument("--max-payment-cents", type=int, default=1000)
    ag.add_argument("--max-daily-cents", type=int, default=2500)
    ag.add_argument("--sync-model", action="store_true",
                    help="an existing profile switches to this profile's model")
    ag.add_argument("--inherit-env", action="store_true",
                    help="also copy this profile's other settings (default: bare)")
    ag.add_argument("--register", action="store_true",
                    help="register it (and its wallet) on the relay now")
    ag.set_defaults(func=cmd_setup_agent)
    rl = ss.add_parser("relay", help="x402 + escrow preset and the operator key")
    rl.add_argument("--preset", default="base-sepolia")
    rl.add_argument("--fee-receiver", default="")
    rl.add_argument("--with", dest="with_networks", default="",
                    help="also take payments on: solana-devnet, tempo-moderato (comma-separated)")
    rl.add_argument("--yes", action="store_true", help="write without asking")
    rl.set_defaults(func=cmd_setup_relay)
    wl = sub.add_parser("wallet", help="an agent's wallets: show, create, register, limits")
    wl.add_argument("action", choices=["show", "create", "register", "limits"])
    wl.add_argument("name", help="the agent's Hermes profile")
    wl.add_argument("--evm", action="store_true", help="create: the EVM wallet (Base, Tempo) — the default")
    wl.add_argument("--solana", action="store_true", help="create: the Solana wallet")
    wl.add_argument("--register", action="store_true", help="create: also register it on the relay")
    wl.add_argument("--networks", default="",
                    help="limits: where it may pay, e.g. base-sepolia,solana-devnet,tempo-moderato")
    wl.add_argument("--max-payment-cents", type=int, default=None)
    wl.add_argument("--max-daily-cents", type=int, default=None)
    wl.add_argument("--offline", action="store_true", help="show: skip the balance lookups")
    wl.set_defaults(func=cmd_wallet)
    db = ss.add_parser("dashboard", help="the Emporia tab in the Hermes dashboard")
    db.add_argument("--hermes-home", default="")
    db.set_defaults(func=cmd_setup_dashboard)
    return p


def main(argv: list[str] | None = None) -> int:
    # The operator's settings (relay URL, escrow, …) before anything reads them.
    from emporia.config import load_profile_dotenv
    load_profile_dotenv()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
