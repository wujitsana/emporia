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

from emporia import __version__

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


def health(timeout: float = 3.0, url: str = "") -> dict | None:
    import httpx
    try:
        r = httpx.get(f"{(url or relay_url()).rstrip('/')}/health", timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


# ─── relay ───────────────────────────────────────────────────────────────────

def _relay_files() -> tuple[Path, Path]:
    home = (this_profile() or REPO) / "home" / ".hermes"
    return home / "emporia-relay.pid", REPO / ".relay.log"


def cmd_relay(args: argparse.Namespace) -> int:
    if args.action == "use":
        return cmd_relay_use(args)
    if args.action == "host":
        return cmd_relay_host(args)
    if args.action in ("link", "unlink", "links"):
        return cmd_relay_links(args)
    if args.action in ("access", "members"):
        return cmd_relay_access(args)
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


# P177: which relay an agent joins. Agents see each other only on the same
# relay (or relays that federate), so the setup offers the one already in use
# first, then running one here, then someone else's.
LOCAL_RELAY = "http://localhost:8088"
RELAY_READING = ("SECURITY.md — what a relay can and cannot do with your agent",
                 "docs/if-a-relay-goes-rogue.md — evidence, re-checking games, taking money back",
                 "docs/RUNBOOK.md — running your own relay for others")


def _relay_brief(h: dict) -> str:
    from emporia.networks import is_testnet, label
    nets = [*(h.get("x402_networks") or []), *([h["tempo_network"]] if h.get("tempo_network") else [])]
    money = ("no wallet payments" if not nets else
             f"pays on {', '.join(label(n) for n in nets)}"
             + ("" if all(is_testnet(n) for n in nets) else " — REAL MONEY"))
    return (f"v{h.get('version', '?')}, {money}, cards {h.get('stripe_mode') or 'off'}, "
            f"fee {h.get('operator_fee_bps', 0) / 100:.1f}%")


def _relays_in_use() -> dict[str, list[str]]:
    """{relay URL: agents using it}, from the agent profiles on this machine."""
    used: dict[str, list[str]] = {}
    for profile, entry in _agent_profiles(profiles_root()):
        url = ((entry.get("env") or {}).get("EMPORIA_RELAY_URL") or "").rstrip("/")
        if url:
            used.setdefault(url, []).append(profile.name)
    return used


def _check_relay_url(url: str) -> str:
    """The URL, cleaned, if a person accepts it; "" if not."""
    import urllib.parse
    url = url.strip().rstrip("/")
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        print("  A relay URL starts with https:// (or http:// on this machine).")
        return ""
    local = parts.hostname in ("localhost", "127.0.0.1", "::1") or installer()._is_local_relay(url)
    if parts.scheme == "http" and not local:
        print("  ! Not https: its sign-in tokens and everything not end-to-end encrypted travel in the "
              "clear. Ask its operator for the https address.")
        if not ask("  Use it anyway?", False):
            return ""
    h = health(timeout=5, url=url)
    if h:
        print(f"  ✓ answering — {_relay_brief(h)}")
    elif not ask("  It is not answering now. Use it anyway?", False):
        return ""
    return url


def _choose_relay(current: str = "") -> str:
    """Ask which relay the agent joins. Returns its URL."""
    used = _relays_in_use()
    candidates = list(dict.fromkeys([u for u in (current, *used, os.getenv("EMPORIA_RELAY_URL", "").rstrip("/"),
                                                 LOCAL_RELAY) if u]))
    up = {u: health(timeout=2, url=u) for u in candidates}
    options, default = [], None
    for u in candidates:
        if not (up[u] or used.get(u) or u == current):
            continue
        who = used.get(u, [])
        desc = (f"{u} — " + (_relay_brief(up[u]) if up[u] else "not answering now")
                + (f"; used by {', '.join(who)}" if who else "") + ("  (its current relay)" if u == current else ""))
        if up[u] and u == LOCAL_RELAY and str(up[u].get("version")) != __version__:
            desc += f" — not this checkout's v{__version__}; restart it: emporia relay stop && emporia relay start"
        options.append((f"use:{u}", desc))
        if default is None and up[u]:
            default = len(options) - 1
    if not up.get(LOCAL_RELAY):
        options.append(("local", "Run a relay on this machine — to test, or to host your own (emporia relay start)"))
    options.append(("remote", "A relay someone else runs — I have its URL"))
    options.append(("host", "Host one for agents on other machines — behind https, in Docker or as a service "
                            "(emporia relay host)"))
    options.append(("later", f"Decide later — {current or LOCAL_RELAY} for now; change any time with "
                             f"`emporia relay use URL NAME`"))
    print("\nWhich relay should it join? Agents meet, play and trade only on the same relay — a linked relay "
          "shows another's listings, but the deal happens on the seller's relay.")
    if not up.get(LOCAL_RELAY) and default is None:
        default = [k for k, _ in options].index("local")
    choice = choose("Relay:", options, default or 0)
    if choice.startswith("use:"):
        return choice[4:]
    if choice == "local":
        return _start_local_relay(current)
    if choice == "host":
        if cmd_relay_host(argparse.Namespace(dir="", domain="", fee_receiver="", admin=None, run="", access="",
                                             names=[])) == 0:
            print("  Then move this agent to it: emporia relay use URL NAME")
        return current or LOCAL_RELAY
    if choice == "remote":
        print("  There is no public directory of relays yet: ask the relay's operator for its URL.")
        print("  Before trusting one, read (also in the dashboard's Docs tab):")
        for line in RELAY_READING:
            print(f"    · {line}")
        for _ in range(3):
            url = _check_relay_url(prompt("Relay URL"))
            if url:
                return url
        print(f"  Keeping {current or LOCAL_RELAY} for now: emporia relay use URL NAME when you have it.")
        return current or LOCAL_RELAY
    return current or LOCAL_RELAY


def _start_local_relay(current: str) -> str:
    """Run a relay on this machine — it listens on 127.0.0.1 only — with its
    testnet payments, then start it. One for other machines is `relay host`."""
    print("  It listens on this machine only. For agents on other machines, run one in Docker behind https:\n"
          "    emporia relay host")
    access = choose("Who may use it?", [
        ("closed", "Only my agents — personal; members only, no links to other relays (recommended)"),
        ("read-only", "My agents act; anyone who can reach it may browse"),
        ("open", "Any agent that can reach it"),
    ], 0)
    if access != "open":
        cmd_relay_access(argparse.Namespace(action="access", url=access, names=[], dir=""))
    if ask("Take testnet USDC payments and escrow on it now (Base Sepolia; needs an address for your fee)?", True):
        if cmd_setup_relay(argparse.Namespace(preset="base-sepolia", with_networks="", fee_receiver="",
                                              yes=False)) != 0:
            print("  Skipped — later: emporia setup relay, then emporia relay stop && emporia relay start")
    os.environ["EMPORIA_RELAY_URL"] = LOCAL_RELAY
    if cmd_relay(argparse.Namespace(action="start", lines=40)) != 0:
        print(f"  Keeping {current or LOCAL_RELAY} for now; once `emporia relay start` works the agent finds it there.")
        return current or LOCAL_RELAY
    return LOCAL_RELAY


def _set_agent_relay(profile: Path, url: str) -> None:
    """Point an agent at `url`: its MCP entry, its profile env and its .env."""
    inst = installer()
    cfg_path = profile / "config.yaml"
    cfg = inst.load_yaml(cfg_path)
    entry = (cfg.get("mcp_servers") or {}).get("emporia") or {}
    entry.setdefault("env", {})["EMPORIA_RELAY_URL"] = url
    if isinstance(cfg.get("env"), dict) and "EMPORIA_RELAY_URL" in cfg["env"]:
        cfg["env"]["EMPORIA_RELAY_URL"] = url
    inst.save_yaml(cfg_path, cfg)
    merge_env_file(profile / ".env", {"EMPORIA_RELAY_URL": url})


def cmd_relay_host(args: argparse.Namespace) -> int:
    """A relay for agents on other machines, with https in front (P183): in
    Docker, or as a system service on a server kept for it. Only a person runs
    it: it opens a relay to the internet. Its settings live in its own folder,
    never in a Hermes profile."""
    import re
    import shutil

    from emporia import relay_host as rh

    if not _persons_terminal():
        print("relay host: only a person runs this, in their own terminal — it opens a relay to other machines.")
        return 1
    ways, why = rh.ways_to_run(rh.in_container(), bool(shutil.which("docker")),
                               Path("/run/systemd/system").is_dir())
    run = args.run
    if not run and len(ways) == 1:
        run = ways[0]
        print(f"\n{why}")
    run = run or choose("How should it run?", [
        ("docker", "In Docker — the relay and https in containers"),
        ("service", "As a system service — a server kept for the relay, no Docker; Caddy installed on it"),
    ], 0)
    if run == "service" and rh.in_container():
        print("relay host: a service here would be this container's, not the server's — use --run docker, "
              "or run `emporia relay host` on the server itself.")
        return 1
    # Docker: beside the profiles, so the host sees it wherever Hermes's data is mounted.
    home = (this_profile() or Path.home()) if run == "docker" else Path.home()
    folder = Path(args.dir or prompt("Folder for it", str(home / "emporia-relay"))).expanduser().resolve()
    print("\nOthers reach it at https://<domain>. Point the domain's DNS (an A record) at this machine first;\n"
          "ports 80 and 443 must be open for the certificate. Leave it blank if you put your own proxy in front.")
    domain = (args.domain or prompt("Domain", "")).strip().lower()
    if domain and not rh.valid_domain(domain):
        print(f"relay host: {domain!r} is not a domain name, like relay.example.com")
        return 1
    where = "in Docker beside it" if run == "docker" else "on this server, as its own user"
    print(f"\nIts operator — the agent that sees its settings and activity in the dashboard. A Hermes agent can run\n"
          f"it {where}: name that agent's profile, or leave it blank.")
    admin_name = (args.admin if args.admin is not None else prompt("Agent profile to run it", "")).strip()
    admin = profiles_root() / admin_name if admin_name else None
    if admin and not (admin / "config.yaml").exists():
        print(f"relay host: no Hermes profile at {admin}")
        return 1
    try:
        if run == "docker":
            written = rh.write_files(folder, REPO, domain, admin)
        else:
            exe = Path(sys.executable).with_name("emporia-relay")
            written = rh.write_service_files(folder, exe, REPO, domain, rh.SERVICE_USER)
    except FileExistsError as e:
        print(f"relay host: {e}")
        return 1
    if admin:
        # Docker: inside the stack the relay is `relay`. Service: the agent reaches it over http, as any client.
        inside = rh.INSIDE_URL if run == "docker" else f"http://127.0.0.1:{rh.LOCAL_PORT}"
        _set_agent_relay(admin, inside)
        print(f"  {admin_name} → {inside}" + (" (inside the stack; run it there, not from the host's Hermes)"
                                             if run == "docker" else ""))
    updates = rh.settings(domain, ask("Accept only agents with a verified Nous sign-in (recommended for a "
                                      "public relay: it is the only check on fake agents)?", True))
    access = getattr(args, "access", "") or choose("Who may use it?", [
        ("open", "Any agent — a public relay"),
        ("read-only", "Anyone may browse its listings; only agents you add act"),
        ("closed", "Only agents you add — private; no links to other relays"),
    ], 0)
    updates["EMPORIA_RELAY_ACCESS"] = access
    if access != "open":
        keys = []
        for ref in ([admin_name] if admin_name else []) + [
                r for r in (args.names or []) if r]:
            try:
                keys.append(_member_key(ref)[0])
            except ValueError as e:
                print(f"  {e}")
        updates["EMPORIA_RELAY_MEMBERS"] = ",".join(keys)
        print(f"  {len(keys)} member(s); add more any time: emporia relay members add NAME|KEY --dir {folder}")
    if run == "service":
        updates.update(rh.service_settings(folder))
    payments = ask("Take testnet USDC payments and escrow (Base Sepolia)?", True)
    if payments:
        fee = args.fee_receiver or prompt("Address that receives your fee (a wallet you control, ideally a Safe)")
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", fee or ""):
            print("  Not an address (0x and 40 hex) — no payments for now; re-run to add them.")
            payments = False
        else:
            updates.update(_BASE_SEPOLIA)
            updates["EMPORIA_ESCROW_FEE_RECEIVER"] = fee
    if admin:
        updates["EMPORIA_AGENT_ID"] = installer().harvest_env(admin).get("EMPORIA_AGENT_ID") or admin_name
    env_path = folder / ".env"
    merge_env_file(env_path, updates)
    env_path.chmod(0o600)
    for path in (*written, env_path):
        print(f"  wrote {path}")
    needs_key = payments and "EMPORIA_OPERATOR_ADDRESS" not in installer().harvest_env(folder)
    done = (_host_docker if run == "docker" else _host_service)(folder, env_path, needs_key, bool(admin), domain)
    if not done:
        return 0 if done is None else 1
    url = updates["EMPORIA_RELAY_URL"]
    for _ in range(40):
        if health(timeout=2, url=f"http://127.0.0.1:{rh.LOCAL_PORT}"):
            break
        time.sleep(1)
    print(f"\n✓ Your relay runs {'in Docker' if run == 'docker' else 'as a system service'} (restarts with the "
          f"machine). Others use {url}")
    print(f"  This machine reaches it at http://127.0.0.1:{rh.LOCAL_PORT}. Move your agents to it:\n"
          f"    emporia relay use {url} NAME …")
    if admin:
        print(f"  {admin_name} is its operator; its dashboard shows the relay's settings and activity.")
    print("  Before others rely on it: docs/RUNBOOK.md (backups of its data, its fee, Stripe).")
    return 0


def _host_steps(steps: list[tuple[str, list[str]]], where: str, intro: str) -> bool | None:
    """Show the commands; run them if the person says so. None: shown only."""
    print(f"\n{intro}\n" + "\n".join(f"    {' '.join(a)}" for _, a in steps))
    if not where or not ask(where, True):
        return None
    return True


def _host_docker(folder: Path, env_path: Path, needs_key: bool, admin: bool, domain: str) -> bool | None:
    import shutil

    from emporia import relay_host as rh

    steps = [("build", ["docker", "compose", "build"])]
    if needs_key:
        steps.append(("operator key", ["docker", "compose", "run", "--rm", "relay",
                                       "python", "-m", "emporia.operator_signer", "generate"]))
    steps.append(("start", ["docker", "compose", *(["--profile", "admin"] if admin else []), "up", "-d"]))
    reachable = bool(shutil.which("docker")) and subprocess.run(
        ["docker", "info"], capture_output=True).returncode == 0
    intro = (f"On the host, in this folder (wherever Hermes's data is mounted there) — {folder}:" if not reachable
             else f"In {folder}:")
    if not reachable:
        print("\nDocker isn't reachable from here — Hermes is probably in a container itself. Docker runs on the host.")
    if not _host_steps(steps, "Build and start it now?" if reachable else "", intro):
        if needs_key:
            print("  then put the operator key's address in its .env as EMPORIA_OPERATOR_ADDRESS=0x…")
        return None
    for what, argv in steps:
        print(f"  {what}…")
        out = subprocess.run(argv, cwd=folder, capture_output=True, text=True)
        if out.returncode != 0:
            print(f"relay host: `{' '.join(argv)}` failed:\n{(out.stderr or out.stdout)[-1500:]}")
            return False
        if what == "operator key":
            address = rh.operator_address(out.stdout)
            merge_env_file(env_path, {"EMPORIA_OPERATOR_ADDRESS": address})
            print(f"  operator key made inside its volume — address {address}; fund it with a little "
                  "Base Sepolia ETH for gas.")
    return True


def _host_service(folder: Path, env_path: Path, needs_key: bool, admin: bool, domain: str) -> bool | None:
    """The relay as its own system user: it alone reads data/; root owns its .env."""
    import shutil

    from emporia import relay_host as rh

    user, data = rh.SERVICE_USER, folder / "data"
    exe = Path(sys.executable).with_name("emporia-relay")
    steps = [("its user", ["sudo", "sh", "-c", f"id -u {user} >/dev/null 2>&1 || useradd --system --no-create-home "
                                               f"--shell /usr/sbin/nologin {user}"]),
             ("its data", ["sudo", "sh", "-c", f"chown -R {user}: {data} && chmod 700 {data}"]),
             ("can it run", ["sudo", "-u", user, "test", "-x", str(exe)])]
    if needs_key:
        steps.append(("operator key", ["sudo", "-u", user, "env", "EMPORIA_NO_DOTENV=1",
                                       f"EMPORIA_KEYS_DIR={data / 'keys'}", sys.executable,
                                       "-m", "emporia.operator_signer", "generate"]))
    steps += [("its settings", ["sudo", "sh", "-c", f"chown root:root {env_path} && chmod 600 {env_path}"]),
              ("start", ["sudo", "sh", "-c", f"cp {folder / rh.SERVICE} /etc/systemd/system/ && systemctl "
                                             "daemon-reload && systemctl enable --now emporia-relay"])]
    runnable = bool(shutil.which("systemctl") and shutil.which("sudo"))
    if not _host_steps(steps, "Run these now (sudo asks for your password)?" if runnable else "",
                       f"On the server, as a person with sudo — {folder}:"):
        if needs_key:
            print("  then put the operator key's address in its .env as EMPORIA_OPERATOR_ADDRESS=0x…")
        _caddy_steps(folder, domain)
        return None
    for what, argv in steps:
        print(f"  {what}…")
        out = subprocess.run(argv, capture_output=what == "operator key", text=True)
        if out.returncode != 0:
            print(f"relay host: `{' '.join(argv)}` failed" + (
                " — the relay's user cannot reach this checkout: put it somewhere outside a private home folder"
                if what == "can it run" else "") + (f":\n{out.stderr[-1500:]}" if out.stderr else ""))
            return False
        if what == "operator key":
            address = rh.operator_address(out.stdout)
            merge_env_file(env_path, {"EMPORIA_OPERATOR_ADDRESS": address})
            print(f"  operator key made in {data / 'keys'} — address {address}; fund it with a little "
                  "Base Sepolia ETH for gas.")
    _caddy_steps(folder, domain)
    if admin:
        print("  The operator agent runs as you, not as the relay's user: its tools cannot read the relay's keys "
              "or change its settings.")
    return True


def _caddy_steps(folder: Path, domain: str) -> None:
    if domain:
        print("\n  https: install Caddy (https://caddyserver.com/docs/install), then\n"
              f"    sudo cp {folder / 'Caddyfile'} /etc/caddy/Caddyfile && sudo systemctl reload caddy")


def _relay_env_path(args: argparse.Namespace) -> Path | None:
    """The relay's .env: a hosted relay's folder (--dir), else this profile's."""
    if getattr(args, "dir", ""):
        return Path(args.dir).expanduser().resolve() / ".env"
    me = this_profile()
    return me / ".env" if me else None


def _links_of(env_path: Path) -> list[str]:
    from emporia.identity import normalize_peer_url
    raw = installer().harvest_env(env_path.parent).get("FEDERATED_RELAYS", "") if env_path.exists() else ""
    return [normalize_peer_url(u) for u in raw.split(",") if u.strip()]


def cmd_relay_links(args: argparse.Namespace) -> int:
    """P184: `emporia relay link URL`, `unlink URL`, `links` — the relays this one
    shares listings (and, when both sides allow it, DMs) with. Each side links
    the other: a link is one-way until the other relay links back."""
    from emporia.identity import normalize_peer_url

    env_path = _relay_env_path(args)
    if env_path is None:
        print("relay links: run it from the relay profile's emporia checkout, or give --dir for a hosted relay")
        return 1
    links = _links_of(env_path)
    if args.action in ("link", "unlink"):
        if not args.url:
            print(f"relay {args.action}: give the other relay's URL — emporia relay {args.action} https://relay.example")
            return 1
        url = normalize_peer_url(args.url)
        if args.action == "link":
            if url in links:
                print(f"relay link: already linked to {url}")
                return 0
            url = _check_relay_url(url) if _TTY else url
            if not url:
                return 1
            links.append(normalize_peer_url(url))
        else:
            if url not in links:
                print(f"relay unlink: not linked to {url}")
                return 1
            links.remove(url)
        try:
            merge_env_file(env_path, {"FEDERATED_RELAYS": ",".join(links)})
        except PermissionError:
            print(f"relay {args.action}: {env_path} belongs to root (a relay run as a service) — set "
                  f"FEDERATED_RELAYS={','.join(links)} there with sudo")
            return 1
        print(f"  {'linked to' if args.action == 'link' else 'unlinked from'} {url}"
              + ("" if args.action == "link" else " — its listings go from here at the relay's next start"))
        print("  Restart the relay to apply it: emporia relay stop && emporia relay start"
              + ("  (or `docker compose restart relay` in its folder)" if getattr(args, "dir", "") else ""))
        if args.action == "link":
            print("  It is one-way until that relay links back to this one; ask its operator.")
        return 0
    # links
    me_url = normalize_peer_url(installer().harvest_env(env_path.parent).get("EMPORIA_RELAY_URL", "")
                                or relay_url())
    if not links:
        print("No linked relays: this relay stands alone. emporia relay link https://relay.example")
        return 0
    import httpx
    for url in links:
        h = health(timeout=3, url=url)
        try:
            theirs = httpx.get(f"{url}/federation/peers", timeout=3).json().get("peers", [])
            back = any(normalize_peer_url(x.get("url", "")) == me_url for x in theirs)
        except (httpx.HTTPError, ValueError):
            back = None
        state = "not answering" if not h else _relay_brief(h)
        both = {True: "linked both ways", False: "one-way — they have not linked back", None: "can't tell"}[back]
        print(f"  {url} — {state}; {both}")
    return 0


def _member_key(ref: str) -> tuple[str, str]:
    """(public key, label) for an agent on this machine, by name — or a key given as is."""
    import re
    if re.fullmatch(r"[0-9a-fA-F]{64}", ref):
        return ref.lower(), ref[:12] + "…"
    agents = {p.name: (p, e) for p, e in _agent_profiles(profiles_root())}
    if ref not in agents:
        raise ValueError(f"no agent '{ref}' on this machine — give its public key (64 hex) instead")
    profile, entry = agents[ref]
    agent_id = (entry.get("env") or {}).get("EMPORIA_AGENT_ID") or ref
    key = _expected_public(profile, agent_id, "identity")
    if not key:   # kept in its .env or linked: ask its own environment, which never prints the key
        r = _as_agent(profile, f"from emporia.identity import read_public_key_hex as r; print(r({agent_id!r}) or '')")
        key = r.stdout.strip() if r.returncode == 0 else ""
    if not key:
        raise ValueError(f"{ref} has no identity key yet (emporia setup agent {ref})")
    return key, ref


def cmd_relay_access(args: argparse.Namespace) -> int:
    """P186: `emporia relay access [open|read-only|closed]` and
    `emporia relay members [add|remove NAME|KEY]` — who may use this relay."""
    from emporia import relay_access as ra

    env_path = _relay_env_path(args)
    if env_path is None:
        print("relay access: run it from the relay profile's emporia checkout, or give --dir for a hosted relay")
        return 1
    env = installer().harvest_env(env_path.parent) if env_path.exists() else {}
    current = env.get(ra.SETTING, "open")
    members = [k for k in env.get(ra.MEMBERS, "").split(",") if k.strip()]
    updates: dict[str, str] = {}
    if args.action == "access":
        wanted = (args.url or "").strip().lower()
        if not wanted:
            print(f"This relay is {current}. Members: {len(members)}.")
            for m, about in (("open", "anyone may join"), ("read-only", "anyone may browse; members act"),
                             ("closed", "members only — personal; no links to other relays")):
                print(f"  {m:10} {about}")
            return 0
        if wanted not in ra.MODES:
            print(f"relay access: one of {', '.join(ra.MODES)}")
            return 1
        updates[ra.SETTING] = wanted
        if wanted != "open" and not members:
            mine = []
            for p, _ in _agent_profiles(profiles_root()):
                try:
                    mine.append(_member_key(p.name)[0])
                except ValueError:
                    pass
            if mine and (not _TTY or ask(f"Make this machine's {len(mine)} agent(s) its members?", True)):
                updates[ra.MEMBERS] = ",".join(mine)
    else:   # members
        verb, refs = (args.url or "list"), args.names
        if verb == "list":
            print(f"This relay is {current}. Members (by key):" if members else
                  f"This relay is {current}. No members listed (its operator agent always is one).")
            names = {}
            for p, _ in _agent_profiles(profiles_root()):
                try:
                    names[_member_key(p.name)[0]] = p.name
                except ValueError:
                    pass
            for k in members:
                print(f"  {k[:16]}…  {names.get(k.lower(), '(not on this machine)')}")
            return 0
        if verb not in ("add", "remove") or not refs:
            print("relay members: add|remove NAME|KEY …, or list")
            return 1
        try:
            keys = [_member_key(r) for r in refs]
        except ValueError as e:
            print(f"relay members: {e}")
            return 1
        for key, label in keys:
            if verb == "add" and key not in members:
                members.append(key)
            elif verb == "remove" and key in members:
                members.remove(key)
            print(f"  {'added' if verb == 'add' else 'removed'} {label}")
        updates[ra.MEMBERS] = ",".join(members)
    try:
        merge_env_file(env_path, updates)
    except PermissionError:
        print(f"relay access: {env_path} belongs to root (a relay run as a service) — set "
              + " ".join(f"{k}={v}" for k, v in updates.items()) + " there with sudo")
        return 1
    if ra.SETTING in updates:
        print(f"  This relay is now {updates[ra.SETTING]}"
              + (" — it stops pulling from and serving linked relays." if updates[ra.SETTING] == "closed" else "."))
    print("  The relay applies it within seconds (one run as a system service: at its next restart)."
          + ("" if updates.get(ra.SETTING) != "closed" else
             "\n  Restart it once so it also stops pulling from linked relays: emporia relay stop && emporia relay start"))
    return 0


def _ensure_member(name: str, url: str) -> None:
    """P186: an agent set up here joins this machine's own read-only or closed relay
    as a member — the person running setup is the one who decides."""
    from emporia import relay_access as ra
    me = this_profile()
    if not me or not installer()._is_local_relay(url) or not (me / ".env").exists():
        return
    env = installer().harvest_env(me)
    if env.get(ra.SETTING, "open") == "open":
        return
    try:
        key, _ = _member_key(name)
    except ValueError:
        return
    members = [k for k in env.get(ra.MEMBERS, "").split(",") if k.strip()]
    if key not in members:
        merge_env_file(me / ".env", {ra.MEMBERS: ",".join([*members, key])})
        print(f"  {name} is now a member of this machine's {env.get(ra.SETTING)} relay.")


def cmd_relay_use(args: argparse.Namespace) -> int:
    """`emporia relay use URL [NAME ...]`: move agents to another relay."""
    agents = {p.name: p for p, _ in _agent_profiles(profiles_root())}
    if not args.url:
        print("relay use: give the relay's URL — emporia relay use https://relay.example NAME")
        return 1
    url = _check_relay_url(args.url) if _TTY else args.url.strip().rstrip("/")
    if not url:
        return 1
    names = args.names or (choose_many("Which agents move to it?", [(n, n) for n in agents], set())
                           if _TTY else [])
    unknown = [n for n in names if n not in agents]
    if not names or unknown:
        print(f"relay use: name the agents ({', '.join(agents) or 'none set up'})"
              + (f"; not an Emporia agent: {', '.join(unknown)}" if unknown else ""))
        return 1
    for n in names:
        _set_agent_relay(agents[n], url)
        print(f"  {n} → {url}")
    print("  Same identity key, so it is the same agent there; it registers on its next start. Its games, "
          "rooms and history stay on the relay it left.")
    installer().print_restart_instructions([agents[n] for n in names])
    return 0


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
    if nous_token.is_current(_NOUS_TOKENS.get(profile) if profile in _NOUS_TOKENS
                             else nous_token.from_hermes(profile) or ""):
        rep.ok("Nous login current (via Hermes)")
    else:
        rep.warn("no current Nous login for this profile — registers key_only, can't enter bounties",
                 f"emporia login --profile {name}")
    from emporia.networks import family, label, resolve_list
    wallet = key_dir / f"{_keyfile_stem(agent_id)}.x402.secp256k1"
    sol_wallet = key_dir / f"{_keyfile_stem(agent_id)}.x402.ed25519"
    pay_nets = resolve_list(dotenv.get("EMPORIA_X402_PAY_NETWORKS", ""))
    # P165: a wallet may be a key file, or kept in the .env / linked (a source).
    has = wallet_presence(profile, agent_id)
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
    from emporia.networks import mainnet_unlocked
    if (has["evm"] or has["svm"]) and mainnet_unlocked(dotenv):
        rep.warn("mainnet unlocked — it may pay real money", f"emporia wallet mainnet {name} --lock")
    if _phrase_state(profile, agent_id) in ("file", "env"):
        rep.warn("its recovery phrase is stored beside its keys — not backed up yet",
                 f"emporia wallet backup {name}   (in your own terminal, never through an agent)")
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


# P174: each Nous check asks Hermes in a process of its own (~0.7s); doctor asks
# for every agent at once instead of one after another.
_NOUS_TOKENS: dict[Path, str] = {}


def _prefetch_nous_tokens(profiles: list[Path]) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from emporia import nous_token
    with ThreadPoolExecutor(max_workers=min(8, len(profiles) or 1)) as pool:
        for profile, token in zip(profiles, pool.map(lambda p: nous_token.from_hermes(p) or "", profiles)):
            _NOUS_TOKENS[profile] = token


def cmd_doctor(args: argparse.Namespace) -> int:
    rep = Report()
    agents = [(p, e) for p, e in _agent_profiles(Path(args.profiles_root) if args.profiles_root
                                                  else profiles_root())
              if not args.profile or p.name == args.profile]
    # Only agents doctor will get as far as checking (a broken MCP entry stops earlier).
    _prefetch_nous_tokens([p for p, e in agents if (e.get("env") or {}).get("EMPORIA_KEYS_DIR")])
    h = doctor_relay(rep)
    for profile, entry in agents:
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
    # P177: which relay it joins — one found, one run here, or a remote one.
    current_relay = ((inst.load_yaml(profile / "config.yaml").get("mcp_servers") or {}).get("emporia") or {}) \
        .get("env", {}).get("EMPORIA_RELAY_URL", "") if existing else ""
    chosen_relay = (args.relay_url or (_choose_relay(current_relay) if _TTY else "")
                    or current_relay or relay_url()).rstrip("/")
    os.environ["EMPORIA_RELAY_URL"] = chosen_relay
    print("\nFirst, its role. This decides which of its own tools could ever reach its keys.")
    # An existing agent keeps its role unless told otherwise: re-running setup
    # (to update its plugin, say) must not quietly take a developer's terminal.
    current_role = None
    if existing:
        _, aid0, _ = _agent(name)
        current_role = profile_role(profile, _has_wallet(profile, aid0))
    keys = [k for k, _ in ROLES]
    role = args.role or choose("What will this agent do?", ROLES,
                               keys.index(current_role) if current_role in keys else 0)
    if role == "developer" and (args.wallet or args.solana):
        print("  ! A developer agent holds no wallet (its tools could reach the keys). Wallet flags ignored.")
        args.wallet = args.solana = False
    if role == "both" and (_backend(profile) if (profile / "config.yaml").exists() else "local") == "local" \
            and _TTY and not ask(
            "  A wallet plus full tools on the `local` backend puts its keys in its tools' reach. Continue?",
            False):
        return 1
    if existing and not args.keys and not _TTY:
        # Without a terminal, an existing agent's key storage is left as it is.
        args.keys = "env" if inst.harvest_env(profile).get("EMPORIA_KEY_STORAGE") == "env" else "files"
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
    from emporia.networks import mainnet_unlocked
    unlocked = existing and mainnet_unlocked(inst.harvest_env(profile))
    if args.pay_networks:
        from emporia.networks import resolve_list
        if _locked_out(resolve_list(args.pay_networks), unlocked, name if existing else ""):
            return 1
        networks = ",".join(resolve_list(args.pay_networks))
    elif families and _TTY:
        networks = ",".join(_choose_networks(families, [], unlocked))
    else:   # P160: what this relay takes, on the wallets it has
        networks = ",".join(_default_networks(families, unlocked))
    wallet = bool(families)
    if wallet and _TTY and not (args.wallet or args.solana):
        args.max_payment_cents = prompt_cents("Largest single payment", args.max_payment_cents)
        args.max_daily_cents = prompt_cents("Most it may spend in 24 hours", args.max_daily_cents)
        args.approve_above_cents = prompt_cents(
            "Ask you before any single payment above (0 = always ask)", args.approve_above_cents)
    inst.create_profile(
        name=name, relay_url=chosen_relay, current_profile_dir=me, inherit_env=args.inherit_env,
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
    _ensure_member(name, chosen_relay or relay_url())
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
    print(f"  Relay       {dotenv.get('EMPORIA_RELAY_URL') or relay_url()}")
    if wallets:
        from emporia.networks import mainnet_unlocked
        print(f"  Pays on     {', '.join(label(n) for n in nets) or 'no network yet'}"
              f"  ({'MAINNET UNLOCKED — real money' if mainnet_unlocked(dotenv) else 'testnets only; mainnet locked'})")
        print(f"  Limits      ${int(dotenv.get('EMPORIA_X402_MAX_PAYMENT_CENTS', 1000)) / 100:.2f} a payment, "
              f"${int(dotenv.get('EMPORIA_X402_MAX_DAILY_CENTS', 5000)) / 100:.2f} a day; "
              + (f"asks you above ${int(approve) / 100:.2f}" if approve else "never asks you"))
    for w in _role_warnings(role, bool(wallets), backend):
        print(f"  ! {w}")
    print(f"  Check it any time: emporia doctor --profile {name}")


def _where_kept(ref: str, has_file: bool) -> str:
    """Where a key lives, in words — never the reference itself."""
    from emporia.key_source import ENV_KEY_NAMES
    if ref.startswith("env:"):
        return "its .env" if ref[4:] in ENV_KEY_NAMES.values() else "an environment variable (linked)"
    if ref.startswith("cmd:"):
        return "a password manager or vault (linked)"
    return "a key file (0600)" if has_file else "not made yet (made at its first start)"


def machine_settings(url: str = "") -> list[dict[str, Any]]:
    """P178: this machine's side — the relay the dashboard reads and what it
    takes — for the Settings view. Read-only, public facts only."""
    h = health(timeout=3, url=url)
    base = (url or relay_url()).rstrip("/")
    local = installer()._is_local_relay(base)
    row = lambda label_, value, about, command="", level="ok": {   # noqa: E731
        "group": "Relay", "label": label_, "value": value, "about": about, "command": command,
        "level": level, "person_only": False}
    out = [row("Relay", f"{base} — " + (_relay_brief(h) if h else "not answering"),
               "The relay this dashboard reads. Agents choose theirs in their own settings.",
               "emporia relay status" if h else ("emporia relay start" if local else ""),
               "ok" if h else "warn")]
    if local:
        out += [row("Its payment rails", "USDC on " + (", ".join(h.get("x402_networks") or []) or "nothing")
                    if h else "—", "What this machine's relay takes: x402 USDC, escrow holds, Tempo.",
                    "emporia setup relay"),
                row("Cards", (h or {}).get("stripe_mode") or "off", "The relay's Stripe key, typed hidden.",
                    "emporia setup stripe")]
    if h and h.get("access") and local:
        out.append(row("Who may use it", h["access"], "open: any agent. read-only: anyone browses, members "
                       "act. closed: members only — personal.", "emporia relay access"))
    out.append({**row("A relay for other machines", "",
                      "Behind https, in Docker or as a system service on a server kept for it; its settings "
                      "in its own folder. This machine's relay listens here only.", "emporia relay host"),
                "person_only": True})
    out += [row("Everything at once", "", "Each component's state and how to set it up; changes nothing.",
                "emporia setup"),
            row("Checks", "", "✓ / ! / ✗ for every agent, with the fix under each problem.", "emporia doctor"),
            row("This tab", "", "Install or refresh the Emporia tab in the Hermes dashboard.",
                "emporia setup dashboard")]
    return out


def agent_settings(profile: Path, agent_id: str) -> list[dict[str, Any]]:
    """P178: every setting `emporia` changes for an agent — its value, what it
    is for, and the command that changes it — for the dashboard's Settings view
    and anything else that wants to show them. Read-only. Nothing secret: key
    *locations* in words, public addresses, numbers. `level` is ok, warn or
    danger; `person_only` marks a command only a person runs, in their own
    terminal."""
    from emporia.key_source import SOURCE_VARS
    from emporia.networks import label, mainnet_unlocked, resolve_list
    name = profile.name
    env = installer().harvest_env(profile) if (profile / ".env").exists() else {}
    has = wallet_presence(profile, agent_id)
    wallet = has["evm"] or has["svm"]
    role, backend = profile_role(profile, wallet), _backend(profile)
    nets = resolve_list(env.get("EMPORIA_X402_PAY_NETWORKS", ""))
    unlocked = mainnet_unlocked(env)
    approve = env.get("EMPORIA_PAYMENT_APPROVAL_CENTS", "")
    phrase = _phrase_state(profile, agent_id)
    cents = lambda var, default: f"${int(env.get(var, default)) / 100:.2f}"   # noqa: E731

    def item(group, label_, value, about, command="", level="ok", person_only=False):
        return {"group": group, "label": label_, "value": value, "about": about, "command": command,
                "level": level, "person_only": person_only}

    out = [
        item("Relay", "Relay", env.get("EMPORIA_RELAY_URL") or relay_url(),
             "Where it meets other agents. Agents see each other only on the same relay (or linked ones).",
             f"emporia relay use URL {name}"),
        item("Role", "Role", role,
             dict(ROLES)[role].split(" — ", 1)[1].split(". ")[0] + ".",
             f"emporia role {name} marketplace|developer|both",
             "danger" if role == "both" and backend == "local" else "ok"),
        item("Role", "Tools turned off", ", ".join(_disabled_toolsets(profile)) or "none (full Hermes tools)",
             "What the role takes away so its own tools cannot reach its keys.", f"emporia role {name}"),
        item("Role", "Terminal backend", backend,
             "local runs its terminal on the same disk as its keys; docker, ssh, modal and daytona do not.",
             "", "warn" if backend == "local" and role != "marketplace" else "ok"),
        item("Keys", "Identity and encryption keys",
             _where_kept(env.get(SOURCE_VARS["identity"], ""), _key_file(profile, agent_id, "identity").exists()),
             "Its identity key signs every move; its encryption key opens sealed DMs.",
             f"emporia identity store {name} --to env"),
    ]
    if wallet:
        for fam, title in (("evm", "EVM wallet"), ("svm", "Solana wallet")):
            if has[fam]:
                out.append(item("Wallets", title, _wallet_address(profile, agent_id, fam == "svm") or "(linked)",
                                "Its public address — where it pays from and is paid. Its key is in "
                                + _where_kept(env.get(SOURCE_VARS[fam], "") or env.get(SOURCE_VARS["phrase"], ""),
                                              True) + ".",
                                f"emporia wallet show {name}"))
        missing = [t for f, t in (("evm", "EVM"), ("svm", "Solana")) if not has[f]]
        if missing:
            out.append(item("Wallets", f"{' and '.join(missing)} wallet", "none",
                            "Add one any time; keys are never replaced.",
                            f"emporia wallet create {name}" + (" --solana" if missing == ["Solana"] else "")))
        out.append(item("Wallets", "Recovery phrase",
                        {"file": "stored beside its keys — not backed up", "env": "stored in its .env — not backed up",
                         "linked": "in your password manager or vault", "": "not kept here"}[phrase],
                        "The backup of both wallets belongs on paper, off this machine.",
                        f"emporia wallet backup {name}" if phrase in ("file", "env") else "",
                        "warn" if phrase in ("file", "env") else "ok", person_only=phrase in ("file", "env")))
        out += [
            item("Payments", "Where it may pay", ", ".join(label(n) for n in nets) or "nowhere yet",
                 "It never pays on any other network.",
                 f"emporia wallet limits {name} --networks base-sepolia,solana-devnet",
                 "ok" if nets else "warn"),
            item("Payments", "Mainnet (real money)", "UNLOCKED" if unlocked else "locked — testnets only",
                 "Only a person unlocks real money, in their own terminal, after a warning.",
                 f"emporia wallet mainnet {name}" + (" --lock" if unlocked else ""),
                 "danger" if unlocked else "ok", person_only=not unlocked),
            item("Payments", "Largest single payment", cents("EMPORIA_X402_MAX_PAYMENT_CENTS", 1000),
                 "A payment above this is refused before it is signed.",
                 f"emporia wallet limits {name} --max-payment-cents 1000"),
            item("Payments", "Most in 24 hours", cents("EMPORIA_X402_MAX_DAILY_CENTS", 5000),
                 "The most a compromised agent can spend in a day.",
                 f"emporia wallet limits {name} --max-daily-cents 2500"),
            item("Payments", "Asks you above", f"${int(approve) / 100:.2f}" if approve else "never asks",
                 "Larger payments wait for your yes, through its own app.",
                 f"emporia wallet limits {name} --approve-above-cents 500", "ok" if approve else "warn"),
        ]
    else:
        out.append(item("Wallets", "Wallets", "none — it cannot pay or be paid",
                        "A complete agent without one: free games, talk, open rooms.",
                        "" if role == "developer" else f"emporia wallet create {name}"))
    if role != "developer":
        out.append(item("Payments", "Card payouts (Stripe)", "see its Stripe status",
                        "Whether it can be paid by card, if the relay takes cards.",
                        f"emporia setup stripe --agent {name}"))
    return out


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


def _store_secret(profile: Path, agent_id: str, kind: str, secret: str,
                  keep_phrase: bool = False) -> subprocess.CompletedProcess:
    """Store a phrase or a key as this agent's wallet, in its own key storage —
    the one way the CLI hands a secret over: on stdin, to the agent's own wallet
    code, never on a command line (P174: was written out three times).
    `kind`: phrase, evm, svm, or derive-evm / derive-svm (the wallet missing
    from a phrase typed again). A phrase is not kept unless `keep_phrase`."""
    env = {"PATH": os.defpath, "HOME": str(Path.home()), **_mcp_env(profile)}
    cmd = {"phrase": ["generate-phrase", "--import-stdin", *(["--keep-phrase"] if keep_phrase else [])],
           "evm": ["import-key"], "svm": ["import-key", "--solana"],
           "derive-evm": ["derive", "--import-stdin"],
           "derive-svm": ["derive", "--import-stdin", "--solana"]}[kind]
    return subprocess.run([sys.executable, "-m", "emporia.agent_wallet", *cmd, "--agent-id", agent_id],
                          env=env, input=secret.strip() + "\n", capture_output=True, text=True, timeout=60)


def wallet_presence(profile: Path, agent_id: str) -> dict[str, bool]:
    """Which wallets an agent has — a key file, or a key or phrase in its .env or
    linked — without reading any key or vault. One check for doctor, the
    overview and roles (P174)."""
    from emporia import agent_wallet as aw
    from emporia.identity import _keyfile_stem
    dotenv = installer().harvest_env(profile) if (profile / ".env").exists() else {}
    key_dir = _key_file(profile, agent_id, "identity").parent
    stem = _keyfile_stem(agent_id)
    phrase = bool(dotenv.get(aw.PHRASE_SOURCE_VAR))
    return {fam: (key_dir / f"{stem}.x402.{suffix}").exists() or phrase or bool(dotenv.get(aw.KEY_SOURCE_VARS[fam]))
            for fam, suffix in (("evm", "secp256k1"), ("svm", "ed25519"))}


def _wallet_address(profile: Path, agent_id: str, solana: bool) -> str | None:
    """The agent's wallet address, worked out here in the same order its own
    wallet uses: a linked source (or the phrase) first, then the key file.
    P174: in-process — a helper process per lookup made `setup` and `doctor`
    take seconds. One is still used for a key linked to a vault command, which
    only the agent's own environment can run."""
    from emporia import agent_wallet as aw
    from emporia.identity import _keyfile_stem
    fam = "svm" if solana else "evm"
    dotenv = installer().harvest_env(profile) if (profile / ".env").exists() else {}
    ref = dotenv.get(aw.KEY_SOURCE_VARS[fam], "").strip()
    phrase_ref = dotenv.get(aw.PHRASE_SOURCE_VAR, "").strip()
    try:
        if ref or phrase_ref:
            use = ref or phrase_ref
            if not use.startswith("env:"):
                r = _wallet_cmd(profile, agent_id, "address", *(["--solana"] if solana else []))
                return r.stdout.strip().splitlines()[-1] if r.returncode == 0 and r.stdout.strip() else None
            secret = dotenv.get(use[4:], "")
            if not secret:
                return None
            if ref:
                raw = aw._parse_secret(fam, secret)
            else:
                evm_raw, sol_raw = aw.derive_from_phrase(secret)
                raw = sol_raw if solana else evm_raw
        else:
            key_dir = _key_file(profile, agent_id, "identity").parent
            f = key_dir / f"{_keyfile_stem(agent_id)}.x402.{'ed25519' if solana else 'secp256k1'}"
            if not f.exists():
                return None
            raw = f.read_bytes()
        if solana:
            from emporia import x402_svm
            return x402_svm.address_of(raw)
        from eth_account import Account
        return Account.from_key(raw).address
    except Exception:  # noqa: BLE001 — a key that does not parse is no wallet to show
        return None


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


# P175: a recovery phrase is shown once, to the person setting up, and then not
# kept: the backup belongs on paper, off the machine whose disk holds the keys.
# Only a person at a terminal may see it — never an agent, whose tool output
# goes to its model provider and its context. The Hermes plugin also refuses
# `emporia wallet backup` from an agent's tools and hides the block between
# these markers from any tool result.
PHRASE_BEGIN = "──── RECOVERY PHRASE ────"
PHRASE_END = "──── END OF RECOVERY PHRASE ────"


def _persons_terminal() -> bool:
    """A person's own terminal: interactive, and not a command an agent's
    terminal tool started (Hermes marks those with its session variables)."""
    return _TTY and not any(k.startswith("HERMES_SESSION_") for k in os.environ)


def _show_phrase(phrase: str, name: str) -> bool:
    """Ask whether to back the phrase up now; if so, show it once and ask for
    three of its words. True when the person has written it down; False keeps
    it stored for `emporia wallet backup NAME`."""
    import getpass
    import secrets
    words = phrase.split()
    print("\n  Both wallets come from one 12-word recovery phrase — their only backup. Backing it")
    print("  up shows it once, for you to write on paper; after that this machine keeps only the")
    print(f"  keys. Not now? It stays stored beside the keys until you run `emporia wallet backup {name}`.")
    if not ask("  Back up the recovery phrase now?", True):
        return False
    print("\n  Write the words on paper, in order. Anyone with them controls both wallets — don't")
    print("  photograph them, paste them into a chat, or give them to an agent.")
    input("  Press Enter to show them… ")
    for _attempt in range(2):
        # The alternate screen: the words leave no scrollback once it closes.
        sys.stdout.write("\x1b[?1049h\x1b[H")
        try:
            print(f"\n  {PHRASE_BEGIN}\n")
            for row in range(0, len(words), 3):
                print("   " + "".join(f"{i + 1:>3}. {words[i]:<12}" for i in range(row, min(row + 3, len(words)))))
            print(f"\n  {PHRASE_END}\n")
            input("  Press Enter once you have written them down… ")
        finally:   # Ctrl-C too: never leave the words on screen
            sys.stdout.write("\x1b[?1049l")
            sys.stdout.flush()
        picks = sorted(secrets.SystemRandom().sample(range(len(words)), 3))
        if all(getpass.getpass(f"  Word #{i + 1} (hidden): ").strip().lower() == words[i] for i in picks):
            print("  ✓ Confirmed.")
            return True
        print("  That does not match.")
        if not ask("  Show the phrase again?", True):
            break
    return False


WALLET_MODES = [
    ("none", "No wallet — card payments only"),
    ("evm", "EVM wallet — USDC on Base, stablecoin on Tempo"),
    ("phrase", "EVM + Solana from one recovery phrase — shown once for you to write down; "
               "imports into MetaMask and Phantom"),
    ("separate", "EVM + Solana with separate keys — a lost or leaked key affects one wallet only"),
    ("solana", "Solana wallet only — USDC on Solana"),
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
        if mode == "import":
            phrase = getpass.getpass("Recovery phrase (hidden; 12 or 24 words): ") if _TTY else ""
            if not phrase.strip():
                print("  No phrase typed; nothing was created.")
                return {f: a for f, a in have.items() if a}
            r = _store_secret(profile, agent_id, "phrase", phrase)
            kept = False
        elif _persons_terminal():
            from emporia import agent_wallet as aw
            phrase = aw.new_phrase()
            kept = not _show_phrase(phrase, profile.name)
            r = _store_secret(profile, agent_id, "phrase", phrase, keep_phrase=kept)
        else:   # nobody here can be shown it: keep it until someone can
            r = _wallet_cmd(profile, agent_id, "generate-phrase")
            kept = True
        if r.returncode != 0:
            print(f"  could not create the wallets: {r.stderr.strip()[-200:]}")
        elif kept:
            print(f"  ! Not backed up yet: the recovery phrase stays stored beside the keys. Back it up any "
                  f"time, in your own terminal (never through an agent):\n"
                  f"      emporia wallet backup {profile.name}\n"
                  f"    `emporia doctor` reminds you until then.")
        else:
            print("  Keys stored. The recovery phrase is not kept on this machine — your copy is the "
                  "only one.")
    else:
        for fam in missing:
            other = "svm" if fam == "evm" else "evm"
            if mode in ("phrase", "solana", "evm") and _phrase_state(profile, agent_id):
                r = _wallet_cmd(profile, agent_id, "derive", *(["--solana"] if fam == "svm" else []))
                if r.returncode == 0:
                    continue
            elif mode in ("phrase", "import") and have[other] and _TTY:
                # P175: the phrase is not kept, so adding the other wallet under it means typing it again.
                typed = getpass.getpass(f"  If the {'EVM' if other == 'evm' else 'Solana'} wallet came from a "
                                        f"recovery phrase, type it to add this one under it (hidden; Enter "
                                        f"for its own key): ")
                if typed.strip():
                    r = _store_secret(profile, agent_id, f"derive-{fam}", typed)
                    if r.returncode == 0:
                        continue
                    print(f"  {r.stderr.strip().removeprefix('error: ')[-200:]} — it gets its own key.")
            _create_wallet(profile, agent_id, fam == "svm")
    return {f: a for f in ("evm", "svm") if (a := _wallet_address(profile, agent_id, f == "svm"))}


def _phrase_state(profile: Path, agent_id: str) -> str:
    """Where the agent's recovery phrase is: "file" or "env" (a copy Emporia
    keeps, not yet backed up), "linked" (the operator's own variable or vault),
    or "" (none kept)."""
    from emporia import agent_wallet as aw
    from emporia.identity import _keyfile_stem
    from emporia.key_source import ENV_KEY_NAMES
    key_dir = _key_file(profile, agent_id, "identity").parent
    if (key_dir / f"{_keyfile_stem(agent_id)}.x402.phrase").exists():
        return "file"
    ref = (installer().harvest_env(profile) if (profile / ".env").exists() else {}).get(aw.PHRASE_SOURCE_VAR, "")
    if not ref.strip():
        return ""
    return "env" if ref.strip() == f"env:{ENV_KEY_NAMES['phrase']}" else "linked"


def _backup_phrase(profile: Path, agent_id: str) -> int:
    """P175: show a stored recovery phrase once, have it confirmed, then remove
    it from this machine. The wallets keep working from their keys."""
    from emporia import agent_wallet as aw
    from emporia.identity import _keyfile_stem
    from emporia.key_source import ENV_KEY_NAMES, SOURCE_VARS, set_env_lines, unset_env_lines
    name = profile.name
    state = _phrase_state(profile, agent_id)
    if state == "linked":
        print(f"{name}'s recovery phrase is linked from your own variable or vault; Emporia keeps no copy.")
        return 0
    if not state:
        print(f"{name} keeps no recovery phrase here — it was backed up already, or its wallets use "
              f"separate keys.")
        return 0
    if not _persons_terminal():
        print("wallet backup: run this yourself, in your own terminal. It shows the recovery phrase, "
              "which must never reach an agent or a log.")
        return 1
    key_dir = _key_file(profile, agent_id, "identity").parent
    phrase_file = key_dir / f"{_keyfile_stem(agent_id)}.x402.phrase"
    dotenv_file = profile / ".env"
    dotenv = installer().harvest_env(profile)
    phrase = " ".join((phrase_file.read_text() if state == "file"
                       else dotenv.get(ENV_KEY_NAMES["phrase"], "")).split()).lower()
    try:
        raws = dict(zip(("evm", "svm"), aw.derive_from_phrase(phrase)))
    except aw.AgentWalletError as e:
        print(f"wallet backup: the stored phrase does not read ({e}); nothing changed.")
        return 1
    before = {f: _wallet_address(profile, agent_id, f == "svm") for f in ("evm", "svm")}
    if not _show_phrase(phrase, name):
        print("Nothing changed: the phrase stays stored until you back it up.")
        return 1
    if state == "env":
        # Its wallets read the phrase; give each the key it gives first.
        set_env_lines(dotenv_file, {k: v for fam in ("evm", "svm") if before[fam]
                                    and not dotenv.get(SOURCE_VARS[fam])
                                    for k, v in ((ENV_KEY_NAMES[fam], aw.env_value(fam, raws[fam])),
                                                 (SOURCE_VARS[fam], f"env:{ENV_KEY_NAMES[fam]}"))})
        unset_env_lines(dotenv_file, [ENV_KEY_NAMES["phrase"], SOURCE_VARS["phrase"]])
    after = {f: _wallet_address(profile, agent_id, f == "svm") for f in ("evm", "svm")}
    if after != before:
        if state == "env":   # put it back as it was: the phrase is the only way to these wallets
            set_env_lines(dotenv_file, {ENV_KEY_NAMES["phrase"]: phrase,
                                        SOURCE_VARS["phrase"]: f"env:{ENV_KEY_NAMES['phrase']}"})
        print("wallet backup: its keys do not give the same wallets as the phrase; the phrase was kept.")
        return 1
    if state == "file":
        phrase_file.unlink()
    scrubbed = _scrub_env_backups(profile, phrase)
    print(f"Removed the recovery phrase from this machine{f' (and {scrubbed} old .env backups)' if scrubbed else ''}. "
          f"Your written copy is now the only one; {name} keeps paying with its keys.")
    if state == "env":
        installer().print_restart_instructions([profile])
    return 0


def _scrub_env_backups(profile: Path, secret: str) -> int:
    """Drop any line holding `secret` from the profile's dated .env backups
    (merge_env_file keeps one per change). Returns how many changed."""
    changed = 0
    for backup in (profile / ".env-backups").glob("*/.env"):
        lines = backup.read_text().splitlines()
        keep = [ln for ln in lines if " ".join(ln.split("=", 1)[-1].split()).lower() != secret]
        if keep != lines:
            backup.write_text("\n".join(keep) + "\n")
            backup.chmod(0o600)
            changed += 1
    return changed


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
    r = _store_secret(profile, agent_id, what, secret)
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
        ("op", "1Password — `op read` (needs OP_SERVICE_ACCOUNT_TOKEN in its .env)"),
        ("bw", "Bitwarden — `bw get` (needs BW_SESSION)"),
        ("vault", "HashiCorp Vault — `vault kv get` (needs VAULT_ADDR, VAULT_TOKEN)"),
        ("aws", "AWS Secrets Manager — `aws secretsmanager` (needs AWS credentials)"),
        ("gcp", "Google Secret Manager — `gcloud secrets` (needs a service account)"),
        ("azure", "Azure Key Vault — `az keyvault secret` (needs a service principal)"),
        ("doppler", "Doppler — `doppler secrets get` (needs DOPPLER_TOKEN)"),
        ("infisical", "Infisical — `infisical secrets get` (needs INFISICAL_TOKEN)"),
        ("env", "An environment variable in its .env"),
        ("cmd", "Another command that prints it"),
    ])
    if where == "op":
        return "cmd:op read " + shlex.quote(prompt("1Password reference (op://Vault/Item/field)"))
    if where == "bw":
        field = choose("Which field?", [("password", "password"), ("notes", "notes")])
        return f"cmd:bw get {field} " + shlex.quote(prompt("Item name or id"))
    if where == "vault":
        return (f"cmd:vault kv get -field={shlex.quote(prompt('Field', 'key'))} "
                + shlex.quote(prompt("Secret path (e.g. secret/emporia/alpha)")))
    if where == "aws":
        return ("cmd:aws secretsmanager get-secret-value --query SecretString --output text --secret-id "
                + shlex.quote(prompt("Secret name or ARN")))
    if where == "gcp":
        return "cmd:gcloud secrets versions access latest --secret=" + shlex.quote(prompt("Secret name"))
    if where == "azure":
        return (f"cmd:az keyvault secret show --query value -o tsv --vault-name "
                f"{shlex.quote(prompt('Vault name'))} --name {shlex.quote(prompt('Secret name'))}")
    if where == "doppler":
        return "cmd:doppler secrets get --plain " + shlex.quote(prompt("Secret name"))
    if where == "infisical":
        return "cmd:infisical secrets get --plain " + shlex.quote(prompt("Secret name"))
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
    from emporia.key_source import KeySourceError, read_text
    try:
        # P173: read with this agent's own environment only — never the CLI's,
        # which may hold another profile's key under the same name.
        secret = read_text(ref, env=installer().harvest_env(profile))
        addresses = {}
        if what == "phrase":
            evm_raw, sol_raw = aw.derive_from_phrase(secret)
            raws = {"evm": evm_raw, "svm": sol_raw}
        else:
            raws = {what: aw._parse_secret(what, secret)}
        for fam in families:
            addresses[fam] = Account.from_key(raws[fam]).address if fam == "evm" else x402_svm.address_of(raws[fam])
    except (KeySourceError, aw.AgentWalletError, ValueError) as e:
        print(f"wallet link: {e}")
        return 1
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


def _choose_networks(families: list[str], current: list[str], mainnet: bool = False) -> list[str]:
    """Where it may pay: ticked are the current ones, else what the relay takes.
    P177: testnets only, unless mainnet is unlocked for this agent."""
    from emporia.networks import NETWORKS, family, is_testnet, label
    h = health() or {}
    offered = list(dict.fromkeys([*(h.get("x402_networks") or []),
                                  *([h["tempo_network"]] if h.get("tempo_network") else [])]))
    candidates = [n for n in dict.fromkeys([*offered, *current,
                                            *(k for k, v in NETWORKS.items() if v.testnet or mainnet)])
                  if family(n) in families and (mainnet or is_testnet(n))]
    ticked = set(current) or set(_default_networks(families, mainnet))
    opts = [(n, f"{label(n)}{'  — this relay takes it' if n in offered else ''}") for n in candidates]
    if not mainnet:
        print("  Testnets only: mainnet (real money) is locked. Its operator can unlock it later, in "
              "their own terminal: emporia wallet mainnet NAME")
    return choose_many("Where may it pay? (it never pays anywhere else)", opts, ticked) if opts else []


def _locked_out(nets: list[str], mainnet: bool, name: str) -> bool:
    """True (and says why) when `nets` names a mainnet network while mainnet is locked."""
    from emporia.networks import is_testnet, label
    live = [n for n in nets if not is_testnet(n)]
    if live and not mainnet:
        print(f"  {', '.join(label(n) for n in live)}: mainnet (real money) is locked for {name or 'a new agent'}. "
              f"Test on testnets first; to unlock, in your own terminal: emporia wallet mainnet {name or 'NAME'}")
        return True
    return False


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
    if how == "link" and found["ref"].startswith("env:") \
            and found["ref"][4:] not in installer().harvest_env(profile):
        # Only this shell has it: the agent's own process will not, so a link
        # would leave it without a wallet. Copy it instead, and say so.
        print(f"  {found['where']} is set in this shell, not in {profile.name}'s .env, so the agent "
              f"could not read it later — copying it instead of linking.")
        how = "copy"
    if how == "link" and found["ref"].startswith("env:"):
        return _link_wallet(profile, agent_id, kind, found["ref"]) == 0
    secret = (Path(found["ref"][5:]).read_text() if found["ref"].startswith("file:")
              else os.environ.get(found["ref"][4:]) or installer().harvest_env(profile).get(found["ref"][4:], ""))
    r = _store_secret(profile, agent_id, kind, secret)
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


def _default_networks(families: list[str], mainnet: bool = False) -> list[str]:
    """What an agent pays on by default: the relay's x402 networks it has a
    wallet for, plus Tempo for an EVM wallet; Base Sepolia (Solana devnet)
    when the relay offers none. Testnets only while mainnet is locked."""
    from emporia.networks import family, is_testnet
    h = health() or {}
    nets = [n for n in (h.get("x402_networks") or []) if family(n) in families]
    if "evm" in families and h.get("tempo_network"):
        nets.append(h["tempo_network"])
    nets = [n for n in nets if mainnet or is_testnet(n)]
    return nets or ((["eip155:84532"] if "evm" in families else [])
                    + (["solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"] if families == ["svm"] else []))


def _pick_agent() -> str:
    names = [p.name for p, _ in _agent_profiles(profiles_root())]
    if not names:
        return ""
    return choose("Which agent?", [(n, n) for n in names])


def cmd_wallet(args: argparse.Namespace) -> int:
    from emporia.networks import NETWORKS, family, label, mainnet_unlocked, resolve_list, stablecoin_balance

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
            ("backup", "Write down its recovery phrase, then remove it from this machine"),
            ("mainnet", "Allow or stop real-money (mainnet) payments — testnets only by default"),
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
            modes = [m for m in WALLET_MODES[1:] if m[0] != "solana" or not before["svm"]]
            mode = choose("Which wallets?", modes, [k for k, _ in modes].index(default))
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

    if args.action == "backup":    # P175: show a stored phrase once, then remove it
        return _backup_phrase(profile, agent_id)

    if args.action == "mainnet":   # P177: real money, only by a person, with a warning
        return _set_mainnet(profile, agent_id, args.name, dotenv, lock=args.lock)

    if args.action == "store":     # P165: key files into the profile .env
        from emporia import agent_wallet as aw
        from emporia.identity import _keyfile_stem
        key_dir = _key_file(profile, agent_id, "identity").parent
        stem = _keyfile_stem(agent_id)
        # P175: the keys only. A recovery phrase is backed up and removed, not moved.
        items = [(fam, f, aw.env_value(fam, f.read_bytes())) for fam, f in
                 (("evm", key_dir / f"{stem}.x402.secp256k1"), ("svm", key_dir / f"{stem}.x402.ed25519"))
                 if f.exists()]
        if not items:
            print(f"wallet store: {args.name} has no wallet key files to move")
            return 1
        rc = _move_to_env(profile, items, True if args.remove_files else None)
        if _phrase_state(profile, agent_id) == "file":
            print(f"  Its recovery phrase is still stored beside the keys. In your own terminal: "
                  f"emporia wallet backup {args.name}")
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
        args.networks = ",".join(_choose_networks(fams, current, mainnet_unlocked(dotenv))) or ""
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
            if _locked_out(nets, mainnet_unlocked(dotenv), args.name):
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
    print(f"  mainnet {'UNLOCKED — it may pay real money' if mainnet_unlocked(dotenv) else 'locked — testnets only'}")
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


def _set_mainnet(profile: Path, agent_id: str, name: str, dotenv: dict[str, str], lock: bool) -> int:
    """P177: let an agent pay real money, or stop it. Unlocking is a person's
    decision, made in their own terminal after a warning; locking is always
    allowed and drops its mainnet networks."""
    from emporia.networks import MAINNET_VAR, is_testnet, label, mainnet_unlocked, resolve_list
    nets = resolve_list(dotenv.get("EMPORIA_X402_PAY_NETWORKS", ""))
    if lock:
        keep = [n for n in nets if is_testnet(n)]
        merge_env_file(profile / ".env", {MAINNET_VAR: "0", "EMPORIA_X402_PAY_NETWORKS": ",".join(keep)})
        dropped = [label(n) for n in nets if n not in keep]
        print(f"{name}: mainnet locked — it pays on testnets only"
              + (f" (no longer on {', '.join(dropped)})" if dropped else "") + ".")
        installer().print_restart_instructions([profile])
        return 0
    if mainnet_unlocked(dotenv):
        print(f"{name} may already pay on mainnet. Lock it again: emporia wallet mainnet {name} --lock")
        return 0
    if not _persons_terminal():
        print("wallet mainnet: run this yourself, in your own terminal — only a person turns on real money.")
        return 1
    role, backend = profile_role(profile, True), _backend(profile)
    if role != "marketplace" and backend == "local":
        print(f"wallet mainnet: {name} has a wallet and a terminal on the same disk as its keys. Make it a "
              f"marketplace agent first (emporia role {name} marketplace), or give it a sandboxed backend.")
        return 1
    cap, day = int(dotenv.get("EMPORIA_X402_MAX_PAYMENT_CENTS", 1000)), int(dotenv.get("EMPORIA_X402_MAX_DAILY_CENTS", 5000))
    approve = dotenv.get("EMPORIA_PAYMENT_APPROVAL_CENTS", "")
    print(f"""
  Mainnet means real money. Before {name} may pay with it:
    · Test on testnets first. Emporia is young and has not been audited.
    · A payment cannot be taken back, and a staked game can be lost. Games of skill for money
      are regulated in many places — check yours.
    · Its caps still apply: ${cap / 100:.2f} a payment, ${day / 100:.2f} a day. Keep them small.
    · {'It asks you before any payment above $' + format(int(approve) / 100, '.2f') if approve
       else 'It never asks you before paying — set an approval threshold below.'}
    · Fund its wallet with only what you are ready to lose.""")
    if not approve or int(approve) > 500:
        merge_env_file(profile / ".env", {"EMPORIA_PAYMENT_APPROVAL_CENTS": str(
            prompt_cents("Ask you before any single payment above (0 = always ask)", 0))})
    if prompt(f"Type the agent's name ({name}) to let it pay real money").strip() != name:
        print("Not unlocked: it still pays on testnets only.")
        return 1
    merge_env_file(profile / ".env", {MAINNET_VAR: "1"})
    fams = [f for f in ("evm", "svm") if _wallet_address(profile, agent_id, f == "svm")]
    chosen = _choose_networks(fams, nets, mainnet=True)
    merge_env_file(profile / ".env", {"EMPORIA_X402_PAY_NETWORKS": ",".join(chosen)})
    print(f"{name}: mainnet unlocked; pays on {', '.join(label(n) for n in chosen) or 'no network'}. "
          f"Lock it again any time: emporia wallet mainnet {name} --lock")
    installer().print_restart_instructions([profile])
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
    return any(wallet_presence(profile, agent_id).values())


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
            argv = ["relay", choose("Relay:", [(a, a) for a in ("status", "start", "stop", "logs")]
                                    + [("host", "host — a relay for other machines, in Docker behind https"),
                                       ("links", "links — the relays this one is linked to")])]
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
        elif _phrase_state(profile, aid) in ("file", "env"):
            how = f"emporia wallet backup {name}   (its recovery phrase is not written down yet)"
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

    r = sub.add_parser("relay", help="start, stop or inspect this machine's relay; `use` moves agents to another; "
                                     "`host` runs one for other machines, in Docker behind https")
    r.add_argument("action", choices=["start", "stop", "status", "logs", "use", "host", "link", "unlink", "links",
                                      "access", "members"])
    r.add_argument("url", nargs="?", default="", help="use, link, unlink: the other relay's URL")
    r.add_argument("names", nargs="*", help="use: the agents that move to it")
    r.add_argument("--lines", type=int, default=40)
    r.add_argument("--dir", default="", help="host: its folder (default ~/emporia-relay); link: a hosted relay's folder")
    r.add_argument("--domain", default="", help="host: the domain others reach it on (https)")
    r.add_argument("--fee-receiver", default="", help="host: the address that receives your fee")
    r.add_argument("--admin", default=None, help="host: a Hermes profile that is its operator")
    r.add_argument("--access", choices=["open", "read-only", "closed"], default="",
                   help="host: who may use it (default: asks; open for a public relay)")
    r.add_argument("--run", choices=["docker", "service"], default="",
                   help="host: in Docker, or as a system service on a server kept for it")
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
                    help="where it may pay, e.g. base-sepolia,solana-devnet (default: what the relay takes; "
                         "testnets only until `emporia wallet mainnet NAME`)")
    ag.add_argument("--relay-url", default="",
                    help="the relay it joins (default: asked; without a terminal, its current one or "
                         "$EMPORIA_RELAY_URL)")
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
                    choices=["", "show", "create", "import", "store", "backup", "mainnet", "register", "limits"])
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
    wl.add_argument("--lock", action="store_true",
                    help="mainnet: lock it again (testnets only; drops mainnet networks)")
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
