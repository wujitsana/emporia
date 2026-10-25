"""emporia — set up, check and run Emporia from one command (P127).

    emporia doctor                     what is set up, what is not, and how to fix it
    emporia relay start|stop|status|logs
    emporia login [--profile NAME]     Nous sign-in for that Hermes profile, verified
    emporia setup agent NAME           a slim agent profile (model + its key + Emporia)
    emporia setup relay                this machine's relay: x402/escrow preset, operator key
    emporia setup dashboard            the Emporia tab in the Hermes dashboard
    emporia role NAME [marketplace|developer|both]  what its tools may reach (marketplace for money)
    emporia identity show|store|link NAME  where its identity and encryption keys are kept
    emporia wallet show|create|import|register|limits NAME
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
import json
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


def prompt(question: str, default: str = "") -> str:
    """A typed answer, or `default` on Enter (and always when not interactive)."""
    if not _TTY:
        return default
    answer = input(f"{question}{f' [{default}]' if default else ''}: ").strip()
    return answer or default


def choose(question: str, options: list[tuple[str, str]], default: int = 0) -> str:
    """One of `options` ((key, label) pairs), by number."""
    if not _TTY:
        return options[default][0]
    print(question)
    for i, (_, text) in enumerate(options, 1):
        print(f"  {i}) {text}")
    while True:
        answer = input(f"Choose 1-{len(options)} [{default + 1}]: ").strip()
        if not answer:
            return options[default][0]
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        print("  Type a number from the list.")


def choose_many(question: str, options: list[tuple[str, str]], ticked: set[str]) -> list[str]:
    """Any of `options`, by numbers; Enter keeps the ticked ones."""
    if not _TTY:
        return [k for k, _ in options if k in ticked]
    print(question)
    for i, (key, text) in enumerate(options, 1):
        print(f"  {i}) [{'x' if key in ticked else ' '}] {text}")
    while True:
        answer = input("Numbers separated by commas (Enter keeps the ticked ones): ").strip()
        if not answer:
            return [k for k, _ in options if k in ticked]
        parts = [x for x in answer.replace(" ", "").split(",") if x]
        if all(x.isdigit() and 1 <= int(x) <= len(options) for x in parts):
            return [options[int(x) - 1][0] for x in parts]
        print("  Type numbers from the list, like 1,3.")


def prompt_cents(question: str, default: int) -> int:
    while True:
        raw = prompt(f"{question} in dollars", f"{default / 100:.2f}").lstrip("$")
        try:
            return round(float(raw) * 100)
        except ValueError:
            print("  A dollar amount, like 10 or 2.50.")


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
    linked_identity = inst.harvest_env(profile).get("EMPORIA_IDENTITY_KEY_SOURCE", "")
    if linked_identity:
        rep.ok("identity key linked (read from its source, not a file)")
    elif not (key_dir / f"{_keyfile_stem(agent_id)}.priv").exists():
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
    # P165: a wallet may be a key file, or kept in the .env / linked (a source).
    phrase_src = bool(dotenv.get("EMPORIA_X402_PHRASE_SOURCE"))
    has = {"evm": wallet.exists() or phrase_src or bool(dotenv.get("EMPORIA_X402_EVM_KEY_SOURCE")),
           "svm": sol_wallet.exists() or phrase_src or bool(dotenv.get("EMPORIA_X402_SOLANA_KEY_SOURCE"))}
    no_key = [n for n in pay_nets if not has.get(family(n) or "", False)]
    if not has["evm"] and not has["svm"]:
        rep.warn("no wallet — can pay by card only", f"emporia wallet create {name}")
    elif not pay_nets:
        rep.fail("a wallet but no EMPORIA_X402_PAY_NETWORKS — it will pay on no network",
                 f"emporia wallet limits {name} --networks base-sepolia")
    elif no_key:
        rep.fail(f"allowed to pay on {', '.join(label(n) for n in no_key)} with no wallet for it",
                 f"emporia wallet create {name}" + (" --solana" if any(family(n) == "svm" for n in no_key) else ""))
    else:
        if not dotenv.get("EMPORIA_PAYMENT_APPROVAL_CENTS"):
            rep.warn("pays up to its caps without ever asking you",
                     f"emporia wallet limits {name} --approve-above-cents 500")
        rep.ok(f"wallet{'s' if has['evm'] and has['svm'] else ''}, pays on "
               f"{', '.join(label(n) for n in pay_nets)}, "
               f"≤ ${int(dotenv.get('EMPORIA_X402_MAX_PAYMENT_CENTS', 1000)) / 100:.2f} each, "
               f"≤ ${int(dotenv.get('EMPORIA_X402_MAX_DAILY_CENTS', 5000)) / 100:.2f}/day")
    # P167/P168: what its own tools can reach. A wallet plus a shell on the
    # keys' disk is the one combination that puts money in reach.
    backend = _backend(profile)
    role = profile_role(profile, has["evm"] or has["svm"])
    if role == "marketplace":
        rep.ok("role marketplace — no terminal, files, code, browser or jobs: its tools cannot reach its keys")
    elif role == "both" and backend == "local":
        rep.fail("has a wallet AND a terminal on the same disk as its keys (backend: local) — a "
                 "prompt-injected agent could read its keys or raise its own limits",
                 f"emporia role {name} marketplace   (or a sandboxed terminal backend: docker, modal, daytona, ssh)")
    elif role == "both":
        rep.warn(f"has a wallet and full tools, sandboxed ({backend}) — keep changes to its running code "
                 f"behind review")
    elif backend == "local":
        rep.warn("developer role on the local backend — its terminal shares a disk with its identity key",
                 "a sandboxed terminal backend, or `emporia identity store "
                 f"{name} --to env` so Hermes hides the key from its tools")
    else:
        rep.ok(f"developer role, sandboxed ({backend}), no wallet")
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
            if has["evm"] and not pay_to:
                rep.warn("EVM wallet not registered as where it is paid", f"emporia wallet register {name}")
            if has["svm"] and not (profile_json.get("x402_pay_to") or {}).get("svm"):
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
    name = args.name or prompt("Agent name (its Hermes profile; lowercase, digits, - or _)")
    if not name:
        print("setup agent: a name is required")
        return 1
    profile = profiles_root() / name
    existing = (profile / "config.yaml").exists()
    print(f"{'Updating' if existing else 'Creating'} agent profile {name} — bare: the model, its one API "
          f"key, and Emporia (MCP, skill, platform plugin, own keys).")
    sync_model = args.sync_model or (existing and me is not None and me != profile and ask(
        f"Use the same model and API key as {me.name if me else 'this profile'}?", True))
    print("\nFirst, its role. This decides which of its own tools could ever reach its keys.")
    role = args.role or choose("What will this agent do?", ROLES, 0)
    if role == "developer" and (args.wallet or args.solana):
        print("  ! A developer agent holds no wallet (its tools could reach the keys). Wallet flags ignored.")
        args.wallet = args.solana = False
    if role == "both" and (_backend(profile) if (profile / "config.yaml").exists() else "local") == "local" \
            and _TTY and not ask(
            "  A wallet plus full tools on the `local` backend puts its keys in its tools' reach. Continue?",
            False):
        return 1
    storage = args.keys or choose("Where should its keys be kept?", [
        ("env", "In its profile .env (recommended) — Hermes hides .env files from the agent's own tools"),
        ("files", "In key files in its keys folder"),
    ])
    # P169: wallet keys the operator already has come first.
    found = byo_candidates(profile) if role != "developer" else []
    if found and not (args.wallet or args.solana or args.use_found_keys):
        print("\nWallet keys already on this machine (bring your own):")
        for f in found:
            print(f"  · {_KIND_NAME[f['kind']]} in {f['where']} → {f['address']}")
    if args.use_found_keys and found:
        mode = "found"
    elif role == "developer":
        mode = "none"
    else:
        mode = _wallet_mode(args, default="found" if found else "none", found=bool(found))
    families = {"none": [], "evm": ["evm"], "solana": ["svm"]}.get(mode, ["evm", "svm"])
    if mode == "byo":
        families = ["evm", "svm"]
    if mode == "found":
        families = sorted({fam for f in found for fam in (("evm", "svm") if f["kind"] == "phrase" else (f["kind"],))})
    if args.pay_networks:
        from emporia.networks import resolve_list
        networks = ",".join(resolve_list(args.pay_networks))
    elif families and _TTY:
        networks = ",".join(_choose_networks(families, []))
    else:   # P160: what this relay takes, on the wallets it has
        networks = ",".join(_default_networks(families))
    wallet = bool(families)
    if wallet and _TTY and not (args.wallet or args.solana):
        args.max_payment_cents = prompt_cents("Largest single payment", args.max_payment_cents)
        args.max_daily_cents = prompt_cents("Most it may spend in 24 hours", args.max_daily_cents)
        args.approve_above_cents = prompt_cents(
            "Ask you before any single payment above (0 = always ask)", args.approve_above_cents)
    inst.create_profile(
        name=name, relay_url=relay_url(), current_profile_dir=me, inherit_env=args.inherit_env,
        sync_model=bool(sync_model), x402_pay_networks=networks,
        x402_max_payment_cents=args.max_payment_cents if wallet else None,
        x402_max_daily_cents=args.max_daily_cents if wallet else None,
    )
    if wallet:   # P166: above this the agent asks its operator, through its own app
        merge_env_file(profile / ".env", {"EMPORIA_PAYMENT_APPROVAL_CENTS": str(args.approve_above_cents)})
    if storage == "env":
        # P165: everything this agent creates from now on — its identity and
        # encryption keys at first start, its wallets now — goes to its .env.
        merge_env_file(profile / ".env", {"EMPORIA_KEY_STORAGE": "env"})
        # The installer made its identity key as a file while building the
        # profile; it is brand new, so it moves without asking.
        _, aid, _ = _agent(name)
        fresh = [(w, f, f.read_bytes().hex()) for w in ("identity", "encryption")
                 if (f := _key_file(profile, aid, w)).exists()]
        if fresh and not existing and not inst.harvest_env(profile).get("EMPORIA_IDENTITY_KEY_SOURCE"):
            _move_to_env(profile, fresh, True)
        elif fresh and existing:
            print(f"  Its existing keys are still files; move them with `emporia identity store {name} "
                  f"--to env` and `emporia wallet store {name}`.")
    if wallet:
        from emporia.networks import NETWORKS, label
        _, agent_id, _ = _agent(name)
        if mode == "found":
            _offer_found_keys(profile, agent_id, args.use_found_keys)
            made = {f: a for f in ("evm", "svm") if (a := _wallet_address(profile, agent_id, f == "svm"))}
        else:
            made = _make_wallets(profile, agent_id, mode)
        for kind, address in made.items():
            print(f"  {'Solana' if kind == 'svm' else 'x402'} wallet: {address}")
        nets = [n for n in networks.split(",") if n]
        print(f"  Pays on {', '.join(label(n) for n in nets) or 'no network'}; at most "
              f"${args.max_payment_cents / 100:.2f} a payment and ${args.max_daily_cents / 100:.2f} a day.")
        for n in nets:
            if n in NETWORKS and NETWORKS[n].faucet:
                print(f"  Fund it on {label(n)}: {NETWORKS[n].faucet}")
    if not args.register and _TTY and health():
        args.register = ask(f"Register {name} on the relay now?", True)
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
    # P169: card payouts — only matters if the relay takes cards.
    if role != "developer" and (health() or {}).get("stripe_enabled") and (
            args.stripe or (_TTY and ask("Should it be paid by card too (Stripe)?", False))):
        _agent_stripe(profile, name)
    # P168: the role decides its tools; then say plainly what was set up.
    apply_role(profile, role)
    _setup_summary(profile, name, role)
    inst.print_restart_instructions(list(inst._TOUCHED_PROFILES))
    return 0


def _setup_summary(profile: Path, name: str, role: str) -> None:
    from emporia.networks import label, resolve_list
    _, agent_id, _ = _agent(name)
    dotenv = installer().harvest_env(profile)
    wallets = [w for w, solana in (("EVM", False), ("Solana", True)) if _wallet_address(profile, agent_id, solana)]
    backend = _backend(profile)
    nets = resolve_list(dotenv.get("EMPORIA_X402_PAY_NETWORKS", ""))
    approve = dotenv.get("EMPORIA_PAYMENT_APPROVAL_CENTS", "")
    print(f"\nSummary for {name}")
    print(f"  Role        {role} — " + dict(ROLES)[role].split(" — ", 1)[1].split(". ")[0])
    print(f"  Tools off   {', '.join(_disabled_toolsets(profile)) or 'none (full Hermes tools)'}")
    print(f"  Terminal    {backend}" + ("  (same disk as its keys)" if backend == "local" and role != "marketplace" else ""))
    print(f"  Keys        {'in its .env (hidden from its tools by Hermes)' if dotenv.get('EMPORIA_KEY_STORAGE') == 'env' else 'key files'}")
    print(f"  Wallets     {', '.join(wallets) or 'none — it cannot pay or be paid (add one any time)'}")
    if wallets:
        print(f"  Pays on     {', '.join(label(n) for n in nets) or 'no network yet'}")
        print(f"  Limits      ${int(dotenv.get('EMPORIA_X402_MAX_PAYMENT_CENTS', 1000)) / 100:.2f} a payment, "
              f"${int(dotenv.get('EMPORIA_X402_MAX_DAILY_CENTS', 5000)) / 100:.2f} a day; "
              + (f"asks you above ${int(approve) / 100:.2f}" if approve else "never asks you"))
    for w in _role_warnings(role, bool(wallets), backend):
        print(f"  ! {w}")
    print(f"  Check it any time: emporia doctor --profile {name}")


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
            # P165: <dir>/.env-backups/<time>/.env — a file named .env, which
            # Hermes's tools refuse to read, since a .env may now hold keys.
            folder = path.parent / ".env-backups" / time.strftime("%Y%m%dT%H%M%S")
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            backup = folder / ".env"
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
    if not args.with_networks and not args.yes and _TTY:
        args.with_networks = ",".join(choose_many(
            "Base Sepolia (USDC and escrow) is always set up. Also take payments on:",
            [("solana-devnet", "Solana devnet — USDC, fees paid by the facilitator"),
             ("tempo-moderato", "Tempo Moderato — stablecoin straight to the payee")], set()))
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


# ─── Stripe (P169) ───────────────────────────────────────────────────────────
# Cards: the relay holds a Stripe secret key; each agent that is PAID by card
# needs a connected account (made at registration in test mode; a hosted
# sign-up link in live mode). An agent PAYING by card needs nothing here: a
# card token (SPT) per purchase, approved by a person in live mode.

_STRIPE_DASHBOARD_STEP = (
    "In the Stripe Dashboard (test mode): Settings → Connect → enable Accounts v1 support "
    "(or complete the Connect platform profile). Without it agents get no connected account and "
    "cannot be paid by card.")


def _stripe_relay_settings(me: Path, args: argparse.Namespace) -> dict[str, str]:
    """The relay's Stripe settings to write. Secrets are typed hidden or read
    from an environment variable — never a command-line argument — and never
    printed."""
    import getpass
    have = installer().harvest_env(me)
    key = os.environ.get(args.stripe_key_env, "") if args.stripe_key_env else ""
    if not key and _TTY:
        state = ("a test key" if have.get("STRIPE_SECRET_KEY", "").startswith(("sk_test_", "rk_test_"))
                 else "a LIVE key" if have.get("STRIPE_SECRET_KEY") else "no key")
        print(f"\nStripe (cards) — this relay has {state}.")
        if not ask("Set or replace the Stripe secret key?", not have.get("STRIPE_SECRET_KEY")):
            return {}
        key = getpass.getpass("Stripe secret key (hidden; sk_test_… while testing): ").strip()
    if not key:
        return {}
    if not key.startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_")):
        print("  ! That is not a Stripe secret key (sk_… or rk_…); nothing written.")
        return {}
    updates = {"STRIPE_SECRET_KEY": key}
    live = "_live_" in key
    if live:
        print("  ! A LIVE key moves real money. Use an sk_test_ key unless this relay is in production.")
        secret = (os.environ.get(args.stripe_webhook_env, "") if args.stripe_webhook_env else
                  getpass.getpass("Webhook signing secret (whsec_…, hidden; required with a live key): ").strip()
                  if _TTY else "")
        if secret:
            updates["STRIPE_WEBHOOK_SECRET"] = secret
        else:
            print("  ! Without STRIPE_WEBHOOK_SECRET the relay refuses to start on a live key.")
    profile_id = args.stripe_profile_id or (prompt(
        "Stripe profile id for agent card tokens (profile_test_…; Enter to skip — cards then need a "
        "pre-made payment intent)") if _TTY else "")
    if profile_id:
        updates["STRIPE_PROFILE_ID"] = profile_id
    print(f"  {_STRIPE_DASHBOARD_STEP}")
    print("  Card money on games and custodial card flows stay off (EMPORIA_CARD_GAME_STAKES, "
          "EMPORIA_CUSTODIAL_CARDS) — see docs/payments.md before turning either on.")
    return updates


def cmd_setup_stripe(args: argparse.Namespace) -> int:
    """This relay's Stripe settings, then (optionally) each agent's card payouts."""
    me = this_profile()
    if me is None:
        print("setup stripe: run it from the relay profile's emporia checkout")
        return 1
    updates = _stripe_relay_settings(me, args)
    if updates:
        changed = merge_env_file(me / ".env", updates)
        print(f"  {len(changed)} Stripe setting(s) written to {me / '.env'} (values not shown)")
        print("  Restart the relay: emporia relay stop && emporia relay start")
    for name in ([args.agent] if args.agent else []):
        _agent_stripe(profiles_root() / name, name)
    return 0


def _agent_stripe(profile: Path, name: str) -> None:
    """Whether this agent can be paid by card, and what to do if not."""
    _, agent_id, env = _agent(name)
    h = health() or {}
    if not h.get("stripe_enabled"):
        print(f"  {name}: this relay takes no cards (no Stripe key) — nothing to do.")
        return
    key_dir = _key_file(profile, agent_id, "identity").parent
    status, me = _signed_get(agent_id, key_dir, f"agents/{agent_id}")
    acct = (me or {}).get("stripe_account_status", "none") if status == 200 else "unknown"
    if acct == "active":
        print(f"  {name}: can be paid by card (connected account active).")
        return
    if h.get("stripe_mode") != "live":
        print(f"  {name}: no active connected account yet. Test mode makes one when it registers.\n"
              f"    {_STRIPE_DASHBOARD_STEP}\n    Then restart its gateway so it registers again.")
        return
    email = prompt(f"  {name}: email of the person who will complete Stripe's sign-up") if _TTY else ""
    if not email:
        print(f"  {name}: live card payouts need Stripe's sign-up — run `emporia setup stripe --agent {name}` "
              f"in a terminal.")
        return
    r = _as_agent(profile, "from emporia import mcp_server as m, json\n"
                           f"print(json.dumps(m.start_stripe_onboarding(agent_id={agent_id!r}, email={email!r})))")
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        out = {"error": r.stderr.strip()[-200:]}
    if out.get("onboarding_url"):
        print(f"  {name}: open this link to finish Stripe's sign-up (identity checks are Stripe's):\n"
              f"    {out['onboarding_url']}")
    else:
        print(f"  {name}: Stripe sign-up could not start: {out.get('error') or out.get('detail')}")


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


WALLET_MODES = [
    ("none", "No wallet — card payments only"),
    ("evm", "EVM wallet — USDC on Base, stablecoin on Tempo"),
    ("phrase", "EVM + Solana from one recovery phrase — one backup; imports into MetaMask and Phantom"),
    ("separate", "EVM + Solana with separate keys — a lost or leaked key affects one wallet only"),
    ("import", "Import a recovery phrase you already have (typed, never shown)"),
    ("byo", "Use any wallet you already have — a phrase, a private key, a key file, an env var, "
            "1Password or Bitwarden; copied in or linked"),
]


def _wallet_mode(args: argparse.Namespace, default: str = "none", found: bool = False) -> str:
    """Which wallets to make: from the flags, else asked."""
    if getattr(args, "import_phrase", False):
        return "import"
    if args.wallet and args.solana:
        return "separate" if args.separate_keys else "phrase"
    if args.solana:
        return "solana"
    if args.wallet:
        return "evm"
    modes = ([("found", "Use the wallet keys found above (recommended — they are already yours)")]
             if found else []) + WALLET_MODES
    keys = [k for k, _ in modes]
    return choose("Which wallets should it have?", modes, keys.index(default) if default in keys else 0)


def _make_wallets(profile: Path, agent_id: str, mode: str) -> dict[str, str]:
    """Create what `mode` asks for; an existing key is kept, never replaced.
    Returns {family: address}."""
    import getpass
    have = {f: _wallet_address(profile, agent_id, f == "svm") for f in ("evm", "svm")}
    if mode == "byo":   # P169: whatever the operator brings, the way they keep it
        _import_wallet(argparse.Namespace(link="", from_env="", key_file="", import_phrase=False,
                                          solana=False), profile, agent_id)
        return {f: a for f in ("evm", "svm") if (a := _wallet_address(profile, agent_id, f == "svm"))}
    want = {"none": [], "evm": ["evm"], "solana": ["svm"], "phrase": ["evm", "svm"],
            "separate": ["evm", "svm"], "import": ["evm", "svm"]}[mode]
    missing = [f for f in want if not have[f]]
    if mode in ("phrase", "import") and len(missing) == 2:
        phrase = ""
        if mode == "import":
            phrase = getpass.getpass("Recovery phrase (hidden; 12 or 24 words): ") if _TTY else ""
            if not phrase.strip():
                print("  No phrase typed; nothing was created.")
                return {f: a for f, a in have.items() if a}
        env = {"PATH": os.defpath, "HOME": str(Path.home()), **_mcp_env(profile)}
        r = subprocess.run([sys.executable, "-m", "emporia.agent_wallet", "generate-phrase",
                            "--agent-id", agent_id, *(["--import-stdin"] if phrase else [])],
                           env=env, input=phrase + "\n" if phrase else None,
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print(f"  could not create the wallets: {r.stderr.strip()[-200:]}")
        else:
            print(f"  Recovery phrase saved (0600) in {profile.name}'s keys folder — the only backup of "
                  f"both wallets. Copy it somewhere safe; Emporia never prints it.")
    else:
        for fam in missing:
            if mode in ("phrase", "solana", "evm") and _has_phrase(profile, agent_id):
                r = _wallet_cmd(profile, agent_id, "derive", *(["--solana"] if fam == "svm" else []))
                if r.returncode == 0:
                    continue
            _create_wallet(profile, agent_id, fam == "svm")
    return {f: a for f in ("evm", "svm") if (a := _wallet_address(profile, agent_id, f == "svm"))}


def _has_phrase(profile: Path, agent_id: str) -> bool:
    from emporia.identity import _keyfile_stem
    key_dir = Path(_mcp_env(profile).get("EMPORIA_KEYS_DIR") or profile / "home" / ".hermes" / "keys")
    return (key_dir / f"{_keyfile_stem(agent_id)}.x402.phrase").exists()


def _import_wallet(args: argparse.Namespace, profile: Path, agent_id: str) -> int:
    """Bring in a wallet the agent (or its operator) already has. Either copy it
    into the keys folder once (a phrase, a key, a key file, an environment
    variable), or link it (P162): the key stays in an environment variable or a
    password manager and is read only when the wallet signs. A secret is typed
    hidden or read from where it lives, handed over on stdin, never printed; an
    existing key is never replaced."""
    import getpass
    what = "phrase" if args.import_phrase else ("svm" if args.solana else "evm")
    if args.link:
        return _link_wallet(profile, agent_id, what, args.link)
    if args.from_env:
        secret = os.environ.get(args.from_env) or installer().harvest_env(profile).get(args.from_env, "")
        if not secret.strip():
            print(f"wallet import: {args.from_env} is not set here or in {profile.name}'s .env")
            return 1
    elif args.key_file:
        secret = Path(args.key_file).expanduser().read_text()
    elif _TTY:
        how = choose("Where is the wallet now?", [
            ("type", "I'll type or paste it (hidden) — a phrase, an EVM key or a Solana key"),
            ("file", "A key file (solana-keygen JSON, or a hex EVM key)"),
            ("env", "An environment variable — copied into the keys folder once"),
            ("link", "1Password, Bitwarden or an environment variable — read each time it signs, "
                     "never stored here"),
            ("receive", "Nothing to import — just be paid at an address I give"),
        ])
        if how == "receive":
            return _register_addresses(profile, agent_id, prompt("EVM address (0x…, Enter to skip)"),
                                       prompt("Solana address (Enter to skip)"))
        what = choose("What is it?", [
            ("phrase", "A recovery phrase (12 or 24 words) — both wallets"),
            ("evm", "An EVM private key (0x…, 64 hex characters)"),
            ("svm", "A Solana secret key (base58 as Phantom exports it, or solana-keygen JSON)"),
        ])
        if how == "link":
            return _link_wallet(profile, agent_id, what, _ask_source())
        if how == "file":
            secret = Path(prompt("Path to the file")).expanduser().read_text()
        elif how == "env":
            name = prompt("Variable name")
            secret = os.environ.get(name) or installer().harvest_env(profile).get(name, "")
        else:
            secret = getpass.getpass("Paste it (hidden): ")
        if not secret.strip():
            print("wallet import: nothing to import")
            return 1
    else:
        print("wallet import: give --key-file, --from-env or --link (with --solana or --import-phrase), "
              "or run it in a terminal")
        return 1
    env = {"PATH": os.defpath, "HOME": str(Path.home()), **_mcp_env(profile)}
    cmd = (["generate-phrase", "--import-stdin"] if what == "phrase"
           else ["import-key", *(["--solana"] if what == "svm" else [])])
    r = subprocess.run([sys.executable, "-m", "emporia.agent_wallet", *cmd, "--agent-id", agent_id],
                       env=env, input=secret if secret.endswith("\n") else secret + "\n",
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"wallet import: {r.stderr.strip().removeprefix('error: ')[-300:]}")
        return 1
    for line in r.stdout.splitlines():
        if line.startswith("Address"):
            print(f"Imported — {line}")
    if args.key_file:
        print(f"  The key is now also in {profile.name}'s keys folder (0600). Delete {args.key_file} "
              f"yourself if it should not stay there.")
    return 0


def _ask_source() -> str:
    """Where a linked key lives, as a source reference (never the secret)."""
    import shlex
    where = choose("Where does it live?", [
        ("op", "1Password — read with `op read op://Vault/Item/field`"),
        ("bw", "Bitwarden — read with `bw get password|notes ITEM`"),
        ("env", "An environment variable the agent's process has"),
        ("cmd", "Another command that prints it"),
    ])
    if where == "op":
        return "cmd:op read " + shlex.quote(prompt("1Password reference (op://Vault/Item/field)"))
    if where == "bw":
        field = choose("Which field?", [("password", "password"), ("notes", "notes")])
        return f"cmd:bw get {field} " + shlex.quote(prompt("Item name or id"))
    if where == "env":
        return "env:" + prompt("Variable name")
    return "cmd:" + prompt("Command")


def _link_wallet(profile: Path, agent_id: str, what: str, ref: str) -> int:
    """Point the wallet at a key source, after reading it once to show the
    address it gives. Only the reference is written to the profile's .env."""
    from eth_account import Account

    from emporia import agent_wallet as aw
    from emporia import x402_svm
    if not ref.startswith(("env:", "cmd:")):
        print("wallet link: a source is env:NAME or cmd:COMMAND")
        return 1
    families = ["evm", "svm"] if what == "phrase" else [what]
    dotenv = installer().harvest_env(profile)
    added = {k: v for k, v in dotenv.items() if k not in os.environ}
    os.environ.update(added)      # an env: source may live in the agent's .env
    try:
        addresses = {}
        for fam in families:
            raw = aw.secret_from_source(fam, f"phrase:{ref}" if what == "phrase" else ref)
            addresses[fam] = Account.from_key(raw).address if fam == "evm" else x402_svm.address_of(raw)
    except aw.AgentWalletError as e:
        print(f"wallet link: {e}")
        return 1
    finally:
        for k in added:
            os.environ.pop(k, None)
    var = aw.PHRASE_SOURCE_VAR if what == "phrase" else aw.KEY_SOURCE_VARS[what]
    merge_env_file(profile / ".env", {var: ref})
    for fam, address in addresses.items():
        print(f"{'Solana' if fam == 'svm' else 'EVM'} wallet linked: {address}  (read when it signs; "
              f"not stored here)")
    if any(_wallet_address(profile, agent_id, f == "svm") for f in families) and \
            any((profile / "home").rglob(f"*.x402.{'secp256k1' if f == 'evm' else 'ed25519'}") for f in families):
        print("  A key file exists too; the linked source is used instead of it.")
    if ref.startswith("cmd:"):
        tool = ref[4:].split()[0] if ref[4:].split() else ""
        print(f"  The agent runs `{tool}` itself each time it starts signing, so {tool} must work in its "
              f"process: 1Password — OP_SERVICE_ACCOUNT_TOKEN in {profile.name}'s .env; Bitwarden — "
              f"BW_SESSION. Then restart its gateway.")
    installer().print_restart_instructions([profile])
    return 0


def _register_addresses(profile: Path, agent_id: str, evm: str, svm: str) -> int:
    """Register payout addresses with no key here: the agent can be paid there,
    and pays from its own wallets (if any) as before."""
    if not (evm or svm):
        print("wallet register: no address given")
        return 1
    if not health():
        print("wallet register: the relay is not answering (emporia relay start)")
        return 1
    code = "from emporia import mcp_server as m\n" + "".join(
        f"w = m.register_x402_wallet(address={a!r}, agent_id={agent_id!r}, network_family={f!r})\n"
        f"print({f!r}, 'registered' if w.get('success') else (w.get('error'), w.get('detail')))\n"
        for f, a in (("evm", evm), ("svm", svm)) if a)
    r = _as_agent(profile, code)
    print("  " + (r.stdout.strip().replace("\n", "\n  ") or r.stderr.strip()[-300:]))
    return 0 if r.returncode == 0 and "registered" in r.stdout else 1


def _choose_networks(families: list[str], current: list[str]) -> list[str]:
    """Where it may pay: ticked are the current ones, else what the relay takes."""
    from emporia.networks import NETWORKS, family, label
    h = health() or {}
    offered = list(dict.fromkeys([*(h.get("x402_networks") or []),
                                  *([h["tempo_network"]] if h.get("tempo_network") else [])]))
    candidates = [n for n in dict.fromkeys([*offered, *current,
                                            *(k for k, v in NETWORKS.items() if v.testnet)])
                  if family(n) in families]
    ticked = set(current) or set(_default_networks(families))
    opts = [(n, f"{label(n)}{'  — this relay takes it' if n in offered else ''}") for n in candidates]
    return choose_many("Where may it pay? (it never pays anywhere else)", opts, ticked) if opts else []


# P169: bring your own keys. Wallet keys an operator already has, under the
# names other agent tools use, or Solana's default key file. Only the address
# each gives is ever shown.
_BYO_ENV = {
    "evm": ("EVM_PRIVATE_KEY", "ETH_PRIVATE_KEY", "WALLET_PRIVATE_KEY", "X402_PRIVATE_KEY", "PRIVATE_KEY"),
    "svm": ("SOLANA_PRIVATE_KEY", "SOL_PRIVATE_KEY"),
    "phrase": ("MNEMONIC", "SEED_PHRASE", "WALLET_MNEMONIC", "RECOVERY_PHRASE"),
}
_BYO_FILES = {"svm": ("~/.config/solana/id.json",)}


def _address_for(kind: str, secret: str) -> str | None:
    """The address a secret gives, or None if it is not one."""
    from emporia import agent_wallet
    try:
        if kind == "phrase":
            evm, sol = agent_wallet.derive_from_phrase(secret)
            from eth_account import Account

            from emporia import x402_svm
            return f"{Account.from_key(evm).address} / {x402_svm.address_of(sol)}"
        raw = agent_wallet._parse_secret(kind, secret)
        if kind == "evm":
            from eth_account import Account
            return Account.from_key(raw).address
        from emporia import x402_svm
        return x402_svm.address_of(raw)
    except Exception:  # noqa: BLE001 — not a key of this kind
        return None


def byo_candidates(profile: Path) -> list[dict[str, str]]:
    """Wallet keys already on this machine: {kind, where, ref, address}. `ref`
    is how to link it (env:NAME) or the file to read; never the secret."""
    found: list[dict[str, str]] = []
    dotenv = installer().harvest_env(profile) if (profile / ".env").exists() else {}
    for kind, names in _BYO_ENV.items():
        for name in names:
            value = os.environ.get(name) or dotenv.get(name)
            if value and (address := _address_for(kind, value)):
                found.append({"kind": kind, "where": f"${name}", "ref": f"env:{name}", "address": address})
    for kind, paths in _BYO_FILES.items():
        for raw_path in paths:
            path = Path(raw_path).expanduser()
            if path.is_file() and (address := _address_for(kind, path.read_text(errors="replace"))):
                found.append({"kind": kind, "where": str(path), "ref": f"file:{path}", "address": address})
    return found


_KIND_NAME = {"evm": "EVM key", "svm": "Solana key", "phrase": "recovery phrase"}


def _use_found(profile: Path, agent_id: str, found: dict[str, str], how: str) -> bool:
    """Link (`env:` only — a file is copied) or copy a found key into the agent's
    storage. Never replaces a key it already has."""
    kind = found["kind"]
    if how == "link" and found["ref"].startswith("env:"):
        return _link_wallet(profile, agent_id, kind, found["ref"]) == 0
    secret = (Path(found["ref"][5:]).read_text() if found["ref"].startswith("file:")
              else os.environ.get(found["ref"][4:]) or installer().harvest_env(profile).get(found["ref"][4:], ""))
    env = {"PATH": os.defpath, "HOME": str(Path.home()), **_mcp_env(profile)}
    cmd = (["generate-phrase", "--import-stdin"] if kind == "phrase"
           else ["import-key", *(["--solana"] if kind == "svm" else [])])
    r = subprocess.run([sys.executable, "-m", "emporia.agent_wallet", *cmd, "--agent-id", agent_id],
                       env=env, input=secret.strip() + "\n", capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"  could not use the {_KIND_NAME[kind]} from {found['where']}: "
              f"{r.stderr.strip().removeprefix('error: ')[-200:]}")
        return False
    print(f"  {_KIND_NAME[kind]} from {found['where']} copied into its key storage ({found['address']})")
    return True


def _offer_found_keys(profile: Path, agent_id: str, choice: str = "") -> list[str]:
    """Offer wallet keys found on this machine; returns the families now set up."""
    found = [f for f in byo_candidates(profile)]
    have = {f for f in ("evm", "svm") if _wallet_address(profile, agent_id, f == "svm")}
    found = [f for f in found if not ({"evm", "svm"} if f["kind"] == "phrase" else {f["kind"]}) <= have]
    if not found:
        return []
    print("\nWallet keys already on this machine (bring your own):")
    for f in found:
        print(f"  · {_KIND_NAME[f['kind']]} in {f['where']} → {f['address']}")
    used: list[str] = []
    for f in found:
        how = choice or (choose(f"Use the {_KIND_NAME[f['kind']]} in {f['where']}?", [
            ("link", "Yes — link to it: read where it is each time it signs, never copied (env vars only)")
            if f["ref"].startswith("env:") else
            ("copy", "Yes — copy it into this agent's key storage"),
            *([("copy", "Yes — copy it into this agent's key storage")] if f["ref"].startswith("env:") else []),
            ("skip", "No"),
        ]) if _TTY else "skip")
        if how == "link" and not f["ref"].startswith("env:"):
            how = "copy"   # a key file is copied; linking reads env vars and commands
        if how in ("link", "copy") and _use_found(profile, agent_id, f, how):
            used += ["evm", "svm"] if f["kind"] == "phrase" else [f["kind"]]
    return used


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


def _pick_agent() -> str:
    names = [p.name for p, _ in _agent_profiles(profiles_root())]
    if not names:
        return ""
    return choose("Which agent?", [(n, n) for n in names])


def cmd_wallet(args: argparse.Namespace) -> int:
    from emporia.networks import NETWORKS, family, label, resolve_list, stablecoin_balance

    if not args.name:
        args.name = _pick_agent() if _TTY else ""
    if not args.name:
        print("wallet: name the agent (emporia wallet show NAME)")
        return 1
    if not args.action:
        args.action = choose(f"{args.name}'s wallets:", [
            ("show", "Show addresses, networks, balances and limits"),
            ("create", "Create a wallet"),
            ("limits", "Change where it may pay and how much"),
            ("import", "Bring in a wallet it already has (phrase, private key, key file)"),
            ("store", "Move its wallet keys into its .env (Hermes hides .env from the agent's tools)"),
            ("register", "Register where it is paid (its wallets, or an address you give)"),
        ]) if _TTY else "show"
    profile, agent_id, env = _agent(args.name)
    if not env:
        print(f"wallet: {args.name} is not an Emporia agent profile under {profiles_root()} "
              f"(emporia setup agent {args.name})")
        return 1
    dotenv = installer().harvest_env(profile)

    if args.action == "create":
        before = {f: _wallet_address(profile, agent_id, f == "svm") for f in ("evm", "svm")}
        args.wallet = args.wallet or args.evm
        default = "solana" if before["evm"] and not before["svm"] else "evm"
        if not (args.wallet or args.solana or args.import_phrase) and _TTY:
            if before["evm"] and before["svm"]:
                print(f"{args.name} already has both wallets; keys are never replaced.")
                return 0
            modes = WALLET_MODES[1:] + ([("solana", "Solana wallet only — USDC on Solana")]
                                        if not before["svm"] else [])
            mode = choose("Which wallets?", modes, [k for k, _ in modes].index(default))
            if mode in ("phrase", "import") and (before["evm"] or before["svm"]):
                print("  The existing wallet has no recovery phrase, so the new one gets its own key.")
        elif not (args.wallet or args.solana or args.import_phrase):
            mode = "evm"
        else:
            mode = _wallet_mode(args)
        made = _make_wallets(profile, agent_id, mode)
        for kind, address in made.items():
            name = "Solana" if kind == "svm" else "EVM"
            print(f"{name} wallet {'(already there)' if before[kind] else 'created'}: {address}")
            if not before[kind]:
                for n in NETWORKS.values():
                    if n.family == kind and n.testnet and n.faucet:
                        print(f"  fund on {label(n.caip2)}: {n.faucet}")
        if not args.register and _TTY and health():
            args.register = ask("Register them on the relay as where it is paid?", True)
        if args.register:
            _register_wallets(profile, agent_id, list(made))
        print(f"Next: `emporia wallet limits {args.name} --networks ...` to choose where it may pay.")
        return 0

    if args.action == "import":
        return _import_wallet(args, profile, agent_id)

    if args.action == "store":     # P165: key files into the profile .env
        from emporia.identity import _keyfile_stem
        key_dir = _key_file(profile, agent_id, "identity").parent
        stem = _keyfile_stem(agent_id)
        phrase = key_dir / f"{stem}.x402.phrase"
        if phrase.exists():   # the phrase alone; both wallets derive from it
            items = [("phrase", phrase, " ".join(phrase.read_text().split()))]
            extra = [f for f in (key_dir / f"{stem}.x402.secp256k1", key_dir / f"{stem}.x402.ed25519") if f.exists()]
        else:
            items = [(fam, f, f.read_bytes().hex()) for fam, f in
                     (("evm", key_dir / f"{stem}.x402.secp256k1"), ("svm", key_dir / f"{stem}.x402.ed25519"))
                     if f.exists()]
            extra = []
        if not items:
            print(f"wallet store: {args.name} has no wallet key files to move")
            return 1
        rc = _move_to_env(profile, items, True if args.remove_files else None)
        for f in extra:   # derived from the phrase, now in the .env
            if rc == 0 and not phrase.exists() and f.exists():
                f.unlink()
                print(f"  removed {f.name} (derived from the phrase)")
        return rc

    if args.action == "register" and (args.address or args.solana_address):
        # Paid at a wallet Emporia holds no key for (a hardware wallet, an app).
        return _register_addresses(profile, agent_id, args.address, args.solana_address)
    if args.action == "register" and _TTY and ask(
            "Be paid at an address you give (a wallet whose key stays elsewhere)?", False):
        return _register_addresses(profile, agent_id, prompt("EVM address (0x…, Enter to skip)"),
                                   prompt("Solana address (Enter to skip)"))
    if args.action == "register":
        kinds = [k for k, solana in (("evm", False), ("svm", True)) if _wallet_address(profile, agent_id, solana)]
        if not kinds:
            print(f"wallet: {args.name} has no wallet to register (emporia wallet create {args.name})")
            return 1
        _register_wallets(profile, agent_id, kinds)
        return 0

    if args.action == "limits" and _TTY and not (args.networks or args.max_payment_cents is not None
                                                  or args.max_daily_cents is not None
                                                  or args.approve_above_cents is not None):
        fams = [f for f in ("evm", "svm") if _wallet_address(profile, agent_id, f == "svm")]
        current = resolve_list(dotenv.get("EMPORIA_X402_PAY_NETWORKS", ""))
        args.networks = ",".join(_choose_networks(fams, current)) or ""
        args.max_payment_cents = prompt_cents(
            "Largest single payment", int(dotenv.get("EMPORIA_X402_MAX_PAYMENT_CENTS", 1000)))
        args.max_daily_cents = prompt_cents(
            "Most it may spend in 24 hours", int(dotenv.get("EMPORIA_X402_MAX_DAILY_CENTS", 2500)))
        args.approve_above_cents = prompt_cents(
            "Ask you before any single payment above (0 = always ask)",
            int(dotenv.get("EMPORIA_PAYMENT_APPROVAL_CENTS", 500)))
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
        if args.approve_above_cents is not None:
            updates["EMPORIA_PAYMENT_APPROVAL_CENTS"] = str(args.approve_above_cents)
        if not updates:
            print("wallet limits: give --networks, --max-payment-cents, --max-daily-cents and/or "
                  "--approve-above-cents")
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
    approve = dotenv.get("EMPORIA_PAYMENT_APPROVAL_CENTS", "")
    print(f"  limits  ${int(dotenv.get('EMPORIA_X402_MAX_PAYMENT_CENTS', 1000)) / 100:.2f} a payment, "
          f"${int(dotenv.get('EMPORIA_X402_MAX_DAILY_CENTS', 5000)) / 100:.2f} a day; "
          + (f"asks you above ${int(approve) / 100:.2f}" if approve else "never asks you"))
    h = health()
    if h:
        offered = [label(n) for n in h.get("x402_networks") or []]
        print(f"  relay   takes USDC on {', '.join(offered) or 'no network'}"
              + (f"; Tempo on {label(h['tempo_network'])}" if h.get("tempo_network") else ""))
    return 0


# ─── roles ───────────────────────────────────────────────────────────────────
# P168: what an agent may do decides what its tools may reach. A wallet and a
# shell on the same disk is the one combination that puts money in reach of a
# prompt-injected agent — so the role is chosen first, and everything after it
# (wallets, tools, warnings) follows from it.

# Hermes toolsets a marketplace agent goes without. Each could reach the keys:
# terminal/file/code directly, the browser through file:// paths, scheduled jobs
# by starting a new agent with full tools. Delegated helpers inherit the cut.
MARKETPLACE_OFF = ["terminal", "file", "code_execution", "computer_use", "browser", "cronjob"]

ROLES = [
    ("marketplace",
     "Marketplace agent (recommended for money) — plays, trades, chats, may hold a wallet. "
     "No terminal, files, code, browser or scheduled jobs, so nothing it runs can reach its keys. "
     "It still improves itself through its skills and memory."),
    ("developer",
     "Developer agent — writes and tests code with full tools, and holds NO wallet, so there is no "
     "money for its tools to reach. Its code reaches a marketplace agent only through review "
     "(a branch you merge), never by editing the running copy."),
    ("both",
     "Both — a wallet AND full tools. Not recommended: on the `local` terminal backend its tools can "
     "read its wallet keys and rewrite its own limits. Only with a sandboxed backend "
     "(docker, modal, daytona, ssh)."),
]


def _disabled_toolsets(profile: Path) -> list[str]:
    cfg = installer().load_yaml(profile / "config.yaml")
    raw = (cfg.get("agent") or {}).get("disabled_toolsets") or []
    return [raw] if isinstance(raw, str) else list(raw)


def _backend(profile: Path) -> str:
    cfg = installer().load_yaml(profile / "config.yaml")
    return str((cfg.get("terminal") or {}).get("backend") or "local")


def profile_role(profile: Path, has_wallet: bool) -> str:
    """The role a profile's settings amount to, whatever it was set up as."""
    if set(MARKETPLACE_OFF) <= set(_disabled_toolsets(profile)):
        return "marketplace"
    return "both" if has_wallet else "developer"


def apply_role(profile: Path, role: str) -> list[str]:
    """Set the profile's Hermes toolsets for `role`; returns the toolsets now off.
    Toolsets the operator switched off for other reasons stay off."""
    inst = installer()
    cfg_path = profile / "config.yaml"
    cfg = inst.load_yaml(cfg_path)
    current = _disabled_toolsets(profile)
    if role == "marketplace":
        new = current + [t for t in MARKETPLACE_OFF if t not in current]
    else:
        new = [t for t in current if t not in MARKETPLACE_OFF]
    if new != current:
        cfg.setdefault("agent", {})["disabled_toolsets"] = new
        inst.save_yaml(cfg_path, cfg)
    return new


def _has_wallet(profile: Path, agent_id: str) -> bool:
    return any(_wallet_address(profile, agent_id, solana) for solana in (False, True))


def _role_warnings(role: str, has_wallet: bool, backend: str) -> list[str]:
    out = []
    if role == "both" and backend == "local":
        out.append("It has a wallet AND a terminal on the same disk as its keys (terminal backend: local). "
                   "A prompt-injected agent could read its keys or raise its own limits. Choose "
                   "`marketplace`, or move its terminal to a sandbox (docker, modal, daytona, ssh).")
    elif role == "both":
        out.append(f"It has a wallet and full tools, sandboxed ({backend}). Keep code changes to the running "
                   f"copy behind review: code that can edit its own signer can lift its own limits.")
    if role == "developer" and has_wallet:
        out.append("A developer agent should hold no wallet, but this one has one. Move its funds and "
                   "stop using the wallet, or make it a marketplace agent.")
    if role == "developer" and backend == "local":
        out.append("Its terminal shares a disk with its identity key (backend: local). Prefer a sandboxed "
                   "backend, or keep its keys in its .env, where Hermes hides them from its tools.")
    return out


def cmd_role(args: argparse.Namespace) -> int:
    if not args.name:
        args.name = _pick_agent() if _TTY else ""
    profile, agent_id, env = _agent(args.name)
    if not env:
        print(f"role: {args.name or '(none)'} is not an Emporia agent profile")
        return 1
    has_wallet = _has_wallet(profile, agent_id)
    current = profile_role(profile, has_wallet)
    backend = _backend(profile)
    print(f"{args.name}: role {current}, terminal backend {backend}, "
          f"{'has a wallet' if has_wallet else 'no wallet'}")
    role = args.role or (choose("Which role?", ROLES, [k for k, _ in ROLES].index(
        "marketplace" if has_wallet else current)) if _TTY else "")
    if not role:
        for w in _role_warnings(current, has_wallet, backend):
            print(f"  ! {w}")
        return 0
    warnings = _role_warnings(role, has_wallet, backend)
    for w in warnings:
        print(f"  ! {w}")
    if role == "both" and backend == "local" and not args.yes and not ask("Set it anyway?", False):
        print("  Nothing changed.")
        return 1
    off = apply_role(profile, role)
    print(f"  role {role}: Hermes toolsets off — {', '.join(off) if off else 'none'}")
    installer().print_restart_instructions([profile])
    return 0


# ─── identity keys ───────────────────────────────────────────────────────────
# P164: an agent's identity (Ed25519) and encryption (X25519) keys may live in a
# password manager instead of files. Linking checks the source gives the SAME
# key — a different one would silently be a different agent.

_KEYS = {"identity": ("EMPORIA_IDENTITY_KEY_SOURCE", ".priv"),
         "encryption": ("EMPORIA_ENCRYPTION_KEY_SOURCE", ".x25519.priv")}


def _key_file(profile: Path, agent_id: str, which: str) -> Path:
    from emporia.identity import _keyfile_stem
    key_dir = Path(_mcp_env(profile).get("EMPORIA_KEYS_DIR") or profile / "home" / ".hermes" / "keys")
    return key_dir / f"{_keyfile_stem(agent_id)}{_KEYS[which][1]}"


def _public_of(which: str, raw: bytes) -> str:
    from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
    cls = ed25519.Ed25519PrivateKey if which == "identity" else x25519.X25519PrivateKey
    return cls.from_private_bytes(raw).public_key().public_bytes_raw().hex()


def _expected_public(profile: Path, agent_id: str, which: str) -> str | None:
    """The public key this agent has now: from its key file, else what the relay has registered."""
    f = _key_file(profile, agent_id, which)
    if f.exists():
        return _public_of(which, f.read_bytes())
    import httpx
    try:
        d = httpx.get(f"{relay_url()}/agents/{agent_id}", timeout=5).json()
    except Exception:  # noqa: BLE001
        return None
    return d.get("public_key_hex" if which == "identity" else "encryption_public_key") or None


def _link_key(profile: Path, agent_id: str, which: str, ref: str) -> int:
    from emporia.key_source import KeySourceError, key32
    want = _expected_public(profile, agent_id, which)
    if not want:
        print(f"identity: no {which} key to match — neither a key file nor a registered key. "
              f"Linking would make a different agent; nothing changed.")
        return 1
    try:
        got = _public_of(which, key32(ref, installer().harvest_env(profile)))
    except (KeySourceError, ValueError) as e:
        print(f"identity: {e}")
        return 1
    if got != want:
        print(f"identity: that source holds a different {which} key (public {got[:12]}…, this agent's is "
              f"{want[:12]}…). Linking it would make a different agent; nothing changed.")
        return 1
    merge_env_file(profile / ".env", {_KEYS[which][0]: ref})
    print(f"{which} key linked: read from the source when needed, public key {got[:12]}… (unchanged)")
    return 0


def _move_to_env(profile: Path, items: list[tuple[str, Path, str]], remove: bool | None) -> int:
    """Move key files into the profile .env: (what, file, value) each. The file
    is removed only after the .env is checked to hold the same value."""
    from emporia import key_source
    moved = []
    for what, f, value in items:
        merge_env_file(profile / ".env", {key_source.ENV_KEY_NAMES[what]: value,
                                          key_source.SOURCE_VARS[what]: f"env:{key_source.ENV_KEY_NAMES[what]}"})
        if installer().harvest_env(profile).get(key_source.ENV_KEY_NAMES[what]) != value:
            print(f"  {what}: the .env did not take it; the key file stays")
            return 1
        moved.append(f)
        print(f"  {what}: now in {profile.name}'s .env")
    merge_env_file(profile / ".env", {"EMPORIA_KEY_STORAGE": "env"})
    if remove is None:
        remove = ask("Remove the key files now? (The .env holds them; a leftover file is readable by the "
                     "agent's tools, which is what this move is for.)", True)
    for f in moved:
        if remove and f.exists():
            f.unlink()
            print(f"  removed {f.name}")
    if not remove:
        print("  The key files are still there; remove them once the agent has restarted fine.")
    installer().print_restart_instructions([profile])
    return 0


def cmd_identity(args: argparse.Namespace) -> int:
    import shlex
    if not args.name:
        args.name = _pick_agent() if _TTY else ""
    profile, agent_id, env = _agent(args.name)
    if not env:
        print(f"identity: {args.name or '(none)'} is not an Emporia agent profile")
        return 1
    if not args.action:
        args.action = choose(f"{args.name}'s identity and encryption keys:", [
            ("show", "Where they are kept"),
            ("store", "Move them into its .env (recommended) or 1Password"),
            ("link", "Read them from a password manager or env var you already put them in"),
        ]) if _TTY else "show"
    dotenv = installer().harvest_env(profile)

    if args.action == "show":
        print(f"Keys for {agent_id}  ({profile})")
        for which, (var, _) in _KEYS.items():
            ref = dotenv.get(var, "")
            f = _key_file(profile, agent_id, which)
            where = ("linked to " + ("an environment variable" if ref.startswith("env:") else
                                     f"`{shlex.split(ref[4:])[0]}`" if ref.startswith("cmd:") else "a source")
                     if ref else ("key file (0600)" if f.exists() else "none yet — made when its tools first start"))
            print(f"  {which:10} {where}" + (f"   (a key file is also here: {f.name})" if ref and f.exists() else ""))
        return 0

    if args.action == "link":
        which = args.key or (choose("Which key?", [("identity", "Identity (Ed25519) — signs every move"),
                                                   ("encryption", "Encryption (X25519) — opens sealed DMs")])
                             if _TTY else "identity")
        ref = args.source or (_ask_source() if _TTY else "")
        if not ref:
            print("identity link: give --source env:NAME or cmd:COMMAND")
            return 1
        return _link_key(profile, agent_id, which, ref)

    to = args.to or (choose("Move them where?", [
        ("env", "Its profile .env (recommended) — Hermes hides .env files from the agent's tools"),
        ("1password", "1Password (as documents; read with `op` when the agent starts)"),
    ]) if _TTY else "env")
    if to == "env":
        items = [(w, f, f.read_bytes().hex()) for w in ("identity", "encryption")
                 if (f := _key_file(profile, agent_id, w)).exists()]
        if not items:
            print("identity store: no key files to move")
            return 1
        return _move_to_env(profile, items, True if args.remove_files else None)
    # into 1Password as documents, then linked
    vault = args.vault or prompt("1Password vault", "Private")
    rc = 0
    for which in ("identity", "encryption"):
        f = _key_file(profile, agent_id, which)
        if not f.exists():
            print(f"  {which}: no key file here, skipped")
            continue
        title = f"emporia {agent_id} {which} key"
        r = subprocess.run(["op", "document", "create", str(f), "--title", title, "--vault", vault],
                           capture_output=True, text=True, timeout=60) if _which("op") else None
        if r is None or r.returncode != 0:
            print(f"  {which}: 1Password did not take it — "
                  f"{'the op CLI is not installed' if r is None else r.stderr.strip()[-200:]}")
            rc = 1
            continue
        ref = "cmd:op document get " + shlex.quote(title) + " --vault " + shlex.quote(vault)
        if _link_key(profile, agent_id, which, ref) != 0:
            rc = 1
            continue
        if ask(f"  Delete the local {which} key file now that 1Password holds it? Keep it until you have "
               f"checked the agent still starts", False):
            f.unlink()
            print(f"  removed {f.name}")
    if rc == 0:
        print(f"  The agent reads them with `op` when it starts: put OP_SERVICE_ACCOUNT_TOKEN (a service "
              f"account with read access to {vault}) in {profile.name}'s .env, then restart its gateway.")
        installer().print_restart_instructions([profile])
    return rc


def _which(cmd: str) -> bool:
    import shutil
    return shutil.which(cmd) is not None


# ─── entry ───────────────────────────────────────────────────────────────────

def cmd_menu(args: argparse.Namespace) -> int:
    """`emporia` with nothing after it: what to do, by number (P161)."""
    if not _TTY:
        build_parser().print_help()
        return 2
    h = health()
    print(f"Emporia — relay {relay_url()} {'answering' if h else 'not answering'}")
    while True:
        what = choose("\nWhat would you like to do?", [
            ("doctor", "Check everything (read-only)"),
            ("agent", "Set up or repair an agent"),
            ("wallet", "An agent's wallets"),
            ("role", "An agent's role: marketplace, developer or both (what its tools may reach)"),
            ("identity", "Where an agent's identity keys are kept (files, 1Password, …)"),
            ("relay-setup", "Set up this machine's relay (payments, operator key)"),
            ("relay", "Start, stop or inspect the relay"),
            ("login", "Nous sign-in for a profile"),
            ("dashboard", "The Emporia tab in the Hermes dashboard"),
            ("quit", "Quit"),
        ])
        if what == "quit":
            return 0
        argv = {"doctor": ["doctor"], "agent": ["setup", "agent"], "wallet": ["wallet"],
                "identity": ["identity"], "role": ["role"],
                "relay-setup": ["setup", "relay"], "login": ["login"],
                "dashboard": ["setup", "dashboard"]}.get(what)
        if what == "relay":
            argv = ["relay", choose("Relay:", [(a, a) for a in ("status", "start", "stop", "logs")])]
        if what == "login":
            argv += ["--profile", _pick_agent()]
        sub = build_parser().parse_args(argv)
        sub.func(sub)


def setup_overview() -> None:
    """Every component, its state, what it is for, and how to set it up (P169).
    Reads only; never prints a secret."""
    from emporia.networks import label
    inst = installer()
    me = this_profile()
    relay_env = inst.harvest_env(me) if me and (me / ".env").exists() else {}
    h = health() or {}

    def line(state: str, name: str, detail: str, how: str = "") -> None:
        print(f"  {state} {name:<23} {detail}" + (f"\n      → {how}" if how else ""))

    print("This machine's relay")
    line("✓" if h else "✗", "Relay", f"answering at {relay_url()}" if h else "not answering", "" if h else "emporia relay start")
    nets = h.get("x402_networks") or []
    line("✓" if nets else "·", "USDC payments", ", ".join(label(n) for n in nets) or "off",
         "" if nets else "emporia setup relay   (Base Sepolia; add --with solana-devnet,tempo-moderato)")
    # A relay running older code may not report these yet: its settings say.
    escrow = h.get("x402_escrow_network") or relay_env.get("EMPORIA_ESCROW_NETWORK")
    line("✓" if escrow else "·", "Escrow holds",
         label(escrow) if escrow else "off — purchases and wagers need it",
         "" if escrow else "emporia setup relay")
    tempo_net = h.get("tempo_network") or relay_env.get("EMPORIA_TEMPO_NETWORK")
    line("✓" if tempo_net else "·", "Tempo", label(tempo_net) if tempo_net else "off",
         "" if tempo_net else "emporia setup relay --with tempo-moderato")
    stripe = ("test mode" if h.get("stripe_mode") == "test" else "LIVE mode" if h.get("stripe_mode") == "live"
              else "off") if h else ("key set" if relay_env.get("STRIPE_SECRET_KEY") else "off")
    line("✓" if stripe not in ("off",) else "·", "Cards (Stripe)",
         stripe + ("" if relay_env.get("STRIPE_PROFILE_ID") or stripe == "off" else "; no profile id (no card tokens)"),
         "emporia setup stripe" if stripe == "off" or not relay_env.get("STRIPE_PROFILE_ID") else "")
    public = relay_url()
    line("✓" if "localhost" not in public and "127.0.0.1" not in public else "·", "Reachable from outside",
         public if "localhost" not in public else "local only — agents on other machines cannot connect",
         "" if "localhost" not in public else "set EMPORIA_RELAY_URL to its public https URL behind a TLS proxy (README § Deployment)")
    peers = [x for x in relay_env.get("FEDERATED_RELAYS", "").split(",") if x.strip()]
    line("✓" if peers else "·", "Federation", f"{len(peers)} peer relay(s)" if peers else "none (optional)",
         "" if peers else "FEDERATED_RELAYS in the relay's .env (optional)")
    mods = relay_env.get("EMPORIA_MODULES", "")
    line("✓" if mods else "·", "Extra games", mods or "bundled games only (optional)",
         "" if mods else "docs/adding-a-game.md (optional)")
    risky = [k for k in ("EMPORIA_CARD_GAME_STAKES", "EMPORIA_CUSTODIAL_CARDS", "EMPORIA_ENGINE_GAME_STAKES")
             if relay_env.get(k, "").lower() in ("1", "true", "yes", "on")]
    line("!" if risky else "✓", "Risky switches", ", ".join(risky) + " ON" if risky else "all off (recommended)")

    print("\nAgents")
    for profile, entry in _agent_profiles(profiles_root()):
        name = profile.name
        aid = (entry.get("env") or {}).get("EMPORIA_AGENT_ID") or name
        wallet = _has_wallet(profile, aid)
        role = profile_role(profile, wallet)
        env = inst.harvest_env(profile)
        bits = [role, "wallet" if wallet else "no wallet",
                "keys in .env" if env.get("EMPORIA_KEY_STORAGE") == "env" else "key files"]
        if wallet:
            bits.append(f"asks above ${int(env['EMPORIA_PAYMENT_APPROVAL_CENTS']) / 100:.2f}"
                        if env.get("EMPORIA_PAYMENT_APPROVAL_CENTS") else "never asks you")
        backend = _backend(profile)
        if role == "both":
            how = f"emporia role {name} marketplace   (a wallet with a terminal on its keys' disk)"
        elif role == "developer" and backend == "local":
            how = (f"a sandboxed terminal backend, or `emporia identity store {name} --to env` "
                   f"(its terminal shares a disk with its identity key)")
        elif wallet and not env.get("EMPORIA_PAYMENT_APPROVAL_CENTS"):
            how = f"emporia wallet limits {name} --approve-above-cents 500   (it pays up to its caps unasked)"
        else:
            how = ""
        line("!" if how else "✓", name, ", ".join(bits), how)
    print("\nDashboard")
    dash = hermes_home() / "plugins" / "emporia"
    line("✓" if dash.exists() else "·", "Emporia tab", "installed" if dash.exists() else "not installed",
         "" if dash.exists() else "emporia setup dashboard")
    print("\nNot available yet (see ROADMAP.md)")
    for name, what in (("AWS KMS signer", "wallet and operator keys the machine never holds"),
                       ("Hosted wallets", "Privy, MetaMask Agent Wallet, Coinbase — limits on their servers"),
                       ("Signing service", "a separate process holding keys and limits, out of the agent's reach"),
                       ("On-chain limits", "spend permissions a stolen key cannot exceed"),
                       ("Key recovery", "replace a compromised identity key and keep its history")):
        line("·", name, what)
    print("\nThe full guide: docs/agent-security-setup.md · check everything: emporia doctor")


def cmd_setup_menu(args: argparse.Namespace) -> int:
    """`emporia setup` with nothing after it: the overview, then what to set up."""
    setup_overview()
    if not _TTY:
        return 0
    what = choose("\nSet up:", [
        ("agent", "An agent — role, keys, wallets, limits, card payouts"),
        ("relay", "This relay's USDC payments (x402, escrow; Solana, Tempo) and operator key"),
        ("stripe", "Cards (Stripe) — the relay's key, and agents' card payouts"),
        ("dashboard", "The Emporia tab in the Hermes dashboard"),
        ("done", "Nothing now"),
    ])
    if what == "done":
        return 0
    sub = build_parser().parse_args(["setup", what])
    return sub.func(sub)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="emporia", description=__doc__.splitlines()[0])
    p.set_defaults(func=cmd_menu)
    sub = p.add_subparsers(dest="command")

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
    st.set_defaults(func=cmd_setup_menu)
    ss = st.add_subparsers(dest="what")
    ag = ss.add_parser("agent", help="create or update a slim agent profile")
    ag.add_argument("name", nargs="?", default="")
    ag.add_argument("--wallet", action="store_true", help="give it an EVM wallet (USDC on Base, Tempo)")
    ag.add_argument("--solana", action="store_true", help="give it a Solana wallet (USDC on Solana)")
    ag.add_argument("--approve-above-cents", type=int, default=500,
                    help="ask the operator before any payment above this (default $5)")
    ag.add_argument("--role", choices=["marketplace", "developer", "both"], default="",
                    help="marketplace (wallet, no terminal/files — recommended for money), developer "
                         "(tools, no wallet) or both (not recommended on the local backend)")
    ag.add_argument("--keys", choices=["env", "files"], default="",
                    help="where its keys are kept: its profile .env (recommended) or key files")
    ag.add_argument("--separate-keys", action="store_true",
                    help="with --wallet --solana: two keys, not one recovery phrase for both")
    ag.add_argument("--import-phrase", action="store_true",
                    help="both wallets from a recovery phrase you type (hidden)")
    ag.add_argument("--stripe", action="store_true", help="set up its card payouts (Stripe) too")
    ag.add_argument("--use-found-keys", choices=["link", "copy"], default="",
                    help="use wallet keys already on this machine ($EVM_PRIVATE_KEY, $SOLANA_PRIVATE_KEY, "
                         "$MNEMONIC, ~/.config/solana/id.json, …): link env vars, or copy them in")
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
    rp = sub.add_parser("role", help="what an agent may do: marketplace, developer or both")
    rp.add_argument("name", nargs="?", default="")
    rp.add_argument("role", nargs="?", default="", choices=["", "marketplace", "developer", "both"])
    rp.add_argument("--yes", action="store_true", help="set `both` on the local backend without asking")
    rp.set_defaults(func=cmd_role)
    idp = sub.add_parser("identity", help="where an agent's identity and encryption keys are kept")
    idp.add_argument("action", nargs="?", default="", choices=["", "show", "store", "link"])
    idp.add_argument("name", nargs="?", default="")
    idp.add_argument("--key", choices=["identity", "encryption"], default="")
    idp.add_argument("--source", default="", help="link: env:NAME or cmd:COMMAND")
    idp.add_argument("--vault", default="", help="store: the 1Password vault")
    idp.add_argument("--to", choices=["env", "1password"], default="", help="store: where to")
    idp.add_argument("--remove-files", action="store_true", help="store --to env: remove the key files after")
    idp.set_defaults(func=cmd_identity)
    wl = sub.add_parser("wallet", help="an agent's wallets: show, create, register, limits")
    wl.add_argument("action", nargs="?", default="",
                    choices=["", "show", "create", "import", "store", "register", "limits"])
    wl.add_argument("name", nargs="?", default="", help="the agent's Hermes profile")
    wl.add_argument("--evm", action="store_true", help="create: the EVM wallet (Base, Tempo) — the default")
    wl.add_argument("--solana", action="store_true", help="create: the Solana wallet")
    wl.add_argument("--wallet", action="store_true", help=argparse.SUPPRESS)
    wl.add_argument("--separate-keys", action="store_true",
                    help="create both: two keys, not one recovery phrase")
    wl.add_argument("--import-phrase", action="store_true",
                    help="create both from a recovery phrase you type (hidden)")
    wl.add_argument("--register", action="store_true", help="create: also register it on the relay")
    wl.add_argument("--networks", default="",
                    help="limits: where it may pay, e.g. base-sepolia,solana-devnet,tempo-moderato")
    wl.add_argument("--max-payment-cents", type=int, default=None)
    wl.add_argument("--max-daily-cents", type=int, default=None)
    wl.add_argument("--approve-above-cents", type=int, default=None,
                    help="limits: ask the operator before any payment above this")
    wl.add_argument("--offline", action="store_true", help="show: skip the balance lookups")
    wl.add_argument("--remove-files", action="store_true", help="store: remove the key files after")
    wl.add_argument("--key-file", default="",
                    help="import: a file holding the key (a solana-keygen JSON with --solana, or a hex EVM key)")
    wl.add_argument("--from-env", default="", help="import: copy the key from this environment variable once")
    wl.add_argument("--link", default="",
                    help="import: don't copy — read the key when signing from env:NAME or cmd:COMMAND "
                         "(e.g. 'cmd:op read op://Vault/Item/key')")
    wl.add_argument("--address", default="", help="register: be paid at this EVM address (no key here)")
    wl.add_argument("--solana-address", default="", help="register: be paid at this Solana address")
    wl.set_defaults(func=cmd_wallet)
    sp = ss.add_parser("stripe", help="cards: this relay's Stripe key, and an agent's card payouts")
    sp.add_argument("--agent", default="", help="also set up this agent's card payouts")
    sp.add_argument("--stripe-key-env", default="", help="read the secret key from this env var (never argv)")
    sp.add_argument("--stripe-webhook-env", default="", help="read the webhook secret from this env var")
    sp.add_argument("--stripe-profile-id", default="")
    sp.set_defaults(func=cmd_setup_stripe)
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
