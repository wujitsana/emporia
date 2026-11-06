"""emporia — set up, check and run Emporia from one command (P127).

    emporia doctor                     what is set up, what is not, and how to fix it
    emporia relay start|stop|status|logs
    emporia login [--profile NAME]     Nous sign-in for that Hermes profile, verified
    emporia setup agent NAME           a slim agent profile (model + its key + Emporia)
    emporia setup relay                this machine's relay: x402/escrow preset, operator key
    emporia setup dashboard [NAME]     refresh an agent's plugin and its dashboard tab
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
import io
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from emporia import hermes_layout
from emporia.cli_hints import localize, via_plugin
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
    """The Hermes profile holding this checkout: .../profiles/<name>/emporia, or
    (P295, installed by `hermes emporia setup`) <home>/plugin-data/emporia/runtime."""
    return hermes_layout.containing_profile(REPO)


def hermes_root() -> Path:
    """The Hermes root (the `default` profile's home) this Emporia belongs to."""
    p = this_profile()
    return hermes_layout.root_of(p) if p else hermes_home()


def profiles_root() -> Path:
    """The folder of named profiles (<root>/profiles)."""
    return hermes_root() / "profiles"


def profile_dir(name: str) -> Path:
    """A profile by Hermes's name for it: `default` is the root itself."""
    return hermes_root() if name == hermes_layout.DEFAULT else profiles_root() / name


def relay_url() -> str:
    return os.getenv("EMPORIA_RELAY_URL", "http://localhost:8088").rstrip("/")


def hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes")


class Cancelled(Exception):
    """Esc (or `b`) where a question has no Back of its own: the command stops,
    as Ctrl-C would, before anything further is changed."""


# P212: the option a menu's Esc means, when it has one.
_BACK_KEYS = ("back", "done", "quit", "cancel")


def heading(title: str, note: str = "") -> None:
    """P220: a section of output — see emporia.menus.heading."""
    from emporia.menus import heading as draw
    draw(title, note)


def _menus():
    from emporia import menus
    return menus if _TTY and menus.available() else None


# ─── wizards: Esc goes back a question (P213) ─────────────────────────────────
# A multi-step command decorated with @wizard records each answer. Esc (with no
# Back option of its own) re-runs it from the top: the earlier answers are
# replayed — printed, no menu opened — and it stops at the previous question,
# with the previous answer preselected. Like Hermes's own setup, made quick:
# what the steps between questions look up (relay health, the signer) is kept
# for the whole wizard, so going back does not wait on the network again.
# Once a wizard starts changing things (wizard_commit), Esc cancels instead —
# replaying past a write would make it twice.

class _Back(Exception):
    pass


_WIZARD: dict | None = None
_NOTHING = object()


def wizard(fn):
    import copy
    import functools

    @functools.wraps(fn)
    def run(args):
        global _WIZARD
        if not _TTY or _WIZARD is not None:      # not interactive, or inside another wizard
            return fn(args)
        replay, prefill, lookups = [], _NOTHING, {}
        try:
            while True:
                _WIZARD = {"answers": [], "replay": list(replay), "prefill": prefill, "open": True,
                           "lookups": lookups, "muted": None}
                if replay or prefill is not _NOTHING:   # P216: going back — its output is on screen already
                    _mute()
                try:
                    return fn(copy.copy(args))
                except _Back:
                    answers = _WIZARD["answers"]
                    if not answers:
                        raise Cancelled() from None
                    replay, prefill = answers[:-1], answers[-1]
                    _unmute()
                    print("  ← back\n")
        finally:
            _unmute()
            _WIZARD = None
    return run


def _mute() -> None:
    """Hide output while earlier answers replay. Anything that asks on its own
    (a hidden secret, a plain input) brings the screen back first."""
    import builtins
    import getpass
    _WIZARD["muted"] = (sys.stdout, builtins.input, getpass.getpass)
    sys.stdout = io.StringIO()

    def speaking(original):
        def ask_anyway(*a, **k):
            _unmute()
            return original(*a, **k)
        return ask_anyway
    builtins.input = speaking(builtins.input)
    getpass.getpass = speaking(getpass.getpass)


def _unmute() -> None:
    import builtins
    import getpass
    if _WIZARD is not None and _WIZARD.get("muted"):
        sys.stdout, builtins.input, getpass.getpass = _WIZARD["muted"]
        _WIZARD["muted"] = None


def wizard_commit() -> None:
    """From here on the wizard changes things: Esc cancels rather than goes back."""
    if _WIZARD is not None:
        _WIZARD["open"] = False


def _wiz_open() -> bool:
    return _WIZARD is not None and _WIZARD["open"]


def _wiz_replay():
    """(True, answer) while replaying earlier answers. After the last one the
    screen speaks again, for the question being asked anew."""
    if _wiz_open() and _WIZARD["replay"]:
        value = _WIZARD["replay"].pop(0)
        if not _WIZARD["replay"]:
            _unmute()
        return True, value
    return False, None


def _wiz_prefill(fallback):
    """The answer last given to the question being asked again, once. Every
    question asked live comes through here: the screen speaks again first."""
    _unmute()
    if _wiz_open() and _WIZARD["prefill"] is not _NOTHING:
        value, _WIZARD["prefill"] = _WIZARD["prefill"], _NOTHING
        return value
    return fallback


def _wiz_record(value):
    if _wiz_open():
        _WIZARD["answers"].append(value)
    return value


def _back() -> None:
    """Esc with no Back option of the question's own."""
    raise _Back() if _wiz_open() else Cancelled()


def _wiz_lookup(key, compute):
    """Within a wizard, look a thing up once — replays reuse it. After its
    first change, look again (a relay it started is now up)."""
    if not _wiz_open():
        return compute()
    cache = _WIZARD["lookups"]
    if key not in cache:
        cache[key] = compute()
    return cache[key]


def _said(question: str, answer: str) -> None:
    """P216: an answer, as one short line and a breath: a label's detail
    (after " — ") was for choosing, not for the record."""
    print(f"{question} → {answer.split(' — ')[0]}\n")


def ask(question: str, default: bool) -> bool:
    if not _TTY:
        return default
    replayed, value = _wiz_replay()
    if replayed:
        _said(question, "yes" if value else "no")
        return _wiz_record(value)
    default = _wiz_prefill(default)
    if m := _menus():
        answer = m.confirm(question, default)
        if answer is m.BACK:
            _back()
        _said(question, "yes" if answer else "no")
        return _wiz_record(answer)
    hint = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {hint}{' (b: back)' if _wiz_open() else ''} ").strip().lower()
    if answer in ("b", "back") and _wiz_open():
        _back()
    return _wiz_record(default if not answer else answer.startswith("y"))


def prompt(question: str, default: str = "") -> str:
    """A typed answer, or `default` on Enter (and always when not interactive).
    In a wizard, Esc then Enter goes back."""
    if not _TTY:
        return default
    replayed, value = _wiz_replay()
    if replayed:
        _said(question, value)
        return _wiz_record(value)
    default = _wiz_prefill(default)
    hint = "  (Esc ⏎ back)" if _wiz_open() else ""
    answer = input(f"{question}{f' [{default}]' if default else ''}{hint}: ").strip()
    if "\x1b" in answer:
        _back()
    print()
    return _wiz_record(answer or default)


def choose(question: str, options: list[tuple[str, str]], default: int = 0) -> str:
    """One of `options` ((key, label) pairs), by number."""
    if not _TTY:
        return options[default][0]
    keys = [k for k, _ in options]
    replayed, value = _wiz_replay()
    if replayed and value in keys:
        _said(question, options[keys.index(value)][1])
        return _wiz_record(value)
    if replayed:            # the options changed since: stop replaying, ask from here, visibly
        _WIZARD["replay"].clear()
        _unmute()
    prior = _wiz_prefill(None)
    default = keys.index(prior) if prior in keys else default
    back = next((k for k in keys if k in _BACK_KEYS), None)
    if m := _menus():
        picked = m.select(question, [text for _, text in options], default)
        if picked is m.BACK:
            if back is None:
                _back()
            picked = keys.index(back)
        _said(question, options[picked][1])
        return _wiz_record(options[picked][0])
    print(question)
    for i, (_, text) in enumerate(options, 1):
        print(f"  {i}) {text}")
    while True:
        answer = input(f"Choose 1-{len(options)} [{default + 1}]: ").strip()
        if not answer:
            return _wiz_record(options[default][0])
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return _wiz_record(options[int(answer) - 1][0])
        if answer.lower() in ("b", "q"):                          # P212: back, without arrows too
            if back is not None:
                return back
            if _wiz_open():
                _back()
        print("  Type a number from the list." + (" (b: back)" if back or _wiz_open() else ""))


def choose_many(question: str, options: list[tuple[str, str]], ticked: set[str]) -> list[str]:
    """Any of `options`, by numbers; Enter keeps the ticked ones."""
    if not _TTY:
        return [k for k, _ in options if k in ticked]
    replayed, value = _wiz_replay()
    if replayed:
        _said(question, ", ".join(t.split(" — ")[0] for k, t in options if k in value) or "none")
        return _wiz_record(value)
    ticked = set(_wiz_prefill(ticked))
    if m := _menus():
        picked = m.checklist(question, [text for _, text in options],
                             {i for i, (k, _) in enumerate(options) if k in ticked})
        if picked is m.BACK and _wiz_open():
            _back()
        chosen = [k for k, _ in options if k in ticked] if picked is m.BACK else [options[i][0] for i in picked]
        _said(question, ", ".join(t.split(" — ")[0] for k, t in options if k in chosen) or "none")
        return _wiz_record(chosen)
    print(question)
    for i, (key, text) in enumerate(options, 1):
        print(f"  {i}) [{'x' if key in ticked else ' '}] {text}")
    while True:
        answer = input("Numbers separated by commas (Enter keeps the ticked ones): ").strip()
        if not answer:
            return _wiz_record([k for k, _ in options if k in ticked])
        if answer.lower() in ("b", "back") and _wiz_open():
            _back()
        parts = [x for x in answer.replace(" ", "").split(",") if x]
        if all(x.isdigit() and 1 <= int(x) <= len(options) for x in parts):
            return _wiz_record([options[int(x) - 1][0] for x in parts])
        print("  Type numbers from the list, like 1,3.")


def prompt_cents(question: str, default: int) -> int:
    while True:
        raw = prompt(f"{question} in dollars", f"{default / 100:.2f}").lstrip("$")
        try:
            return round(float(raw) * 100)
        except ValueError:
            print("  A dollar amount, like 10 or 2.50.")


class Report:
    """✓ / ! / ✗ lines with a fix under each problem; exit status 1 on any ✗.

    P219: `compact` (what `emporia doctor` uses, unless --all) keeps a section's
    passing checks to one line — "✓ 8 checks pass" — and spells out only what
    needs a look; lines wrap to the terminal, a fix sits under its line as
    "→ …", and in a terminal ✓ ! ✗ take the theme's green, yellow and red
    (NO_COLOR: none). Without `compact`, every line prints as it comes."""

    def __init__(self, compact: bool = False) -> None:
        self.failed = False
        self.compact = compact
        self._title: str | None = None
        self._items: list[tuple[str, str, str]] = []
        tty = sys.stdout.isatty() and not os.getenv("NO_COLOR")
        self._paint = (lambda text, code: f"\x1b[{code}m{text}\x1b[0m") if tty else (lambda text, code: text)

    def section(self, title: str) -> None:
        if not self.compact:
            print(f"\n{title}")
            return
        self.flush()
        self._title = title

    def ok(self, msg: str) -> None:
        self._add("✓", msg, "")

    def warn(self, msg: str, fix: str = "") -> None:
        self._add("!", msg, fix)

    def fail(self, msg: str, fix: str = "") -> None:
        self.failed = True
        self._add("✗", msg, fix)

    def _add(self, mark: str, msg: str, fix: str) -> None:
        fix = localize(fix)        # `hermes emporia …` where the plugin installed this runtime
        if not self.compact:
            print(f"  {mark} {msg}" + (f"\n      fix: {fix}" if fix else ""))
            return
        self._items.append((mark, msg, fix))

    def flush(self) -> None:
        """Print the section gathered so far (compact only)."""
        import shutil
        import textwrap
        if not self.compact or (self._title is None and not self._items):
            return
        width = max(40, shutil.get_terminal_size((100, 24)).columns - 1)
        colour = {"✓": "32", "!": "33", "✗": "31"}
        if self._title is not None:      # the title, then a quiet rule to the edge
            heading(self._title)
        passed = [i for i in self._items if i[0] == "✓"]
        shown = [i for i in self._items if i[0] != "✓"]
        if passed:
            print("  " + self._paint("✓", colour["✓"]) + (
                f" {passed[0][1]}" if len(passed) == 1 else f" {len(passed)} checks pass"))
        for mark, msg, fix in sorted(shown, key=lambda i: i[0] != "✗"):
            # "  ! " is four columns: wrapped lines start under the text, not the mark
            lines = textwrap.wrap(msg, width - 4) or [""]
            print("  " + self._paint(mark, colour[mark]) + " " + lines[0])
            for more in lines[1:]:
                print("    " + more)
            if fix:      # "    → " is six columns: its wrapped lines line up under the fix
                for i, line in enumerate(textwrap.wrap(fix, width - 6)):
                    print(("    → " if i == 0 else "      ") + self._paint(line, "2"))
        self._title, self._items = None, []


def health(timeout: float = 3.0, url: str = "") -> dict | None:
    import httpx

    def look() -> dict | None:
        try:
            r = httpx.get(f"{(url or relay_url()).rstrip('/')}/health", timeout=timeout)
            return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None
    return _wiz_lookup(("health", url or relay_url()), look)


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
    if args.action == "move":
        return cmd_relay_move(args)
    if args.action == "backup":
        return cmd_relay_backup(args)
    if args.action == "rotate-key":
        return cmd_relay_rotate_key(args)
    if args.action == "rotate-sign-in-secret":
        return cmd_relay_rotate_sign_in_secret(args)
    if args.action == "config":
        return cmd_relay_config(args)
    if args.action == "modules":
        return cmd_relay_modules(args)
    if args.action == "overview":
        return cmd_relay_overview(args)
    if args.action == "owner-key":
        return cmd_relay_owner_key(args)
    if args.action == "remove":
        return cmd_relay_remove(args)
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
    if args.action == "restart":
        # P229: new relay code takes effect only in a new process. Stop, wait
        # until the port is free, start.
        if cmd_relay(argparse.Namespace(**{**vars(args), "action": "stop"})) != 0:
            return 1
        for _ in range(40):
            if not health(timeout=1):
                break
            time.sleep(0.25)
        return cmd_relay(argparse.Namespace(**{**vars(args), "action": "start"}))
    if args.action == "stop":
        try:
            pid = int(pid_file.read_text())
            if not _is_relay_pid(pid):        # P285: after a reboot the number may be another process's
                pid_file.unlink(missing_ok=True)
                raise ProcessLookupError(pid)
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
    return _start_relay()


def _is_our_pid(pid: int, *marks: bytes) -> bool:
    """Whether `pid` runs a command containing one of `marks` (not a number reused after a reboot)."""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except OSError:
        return False
    return any(m in cmd for m in marks)


def _is_relay_pid(pid: int) -> bool:
    """Whether `pid` is a relay `emporia relay start` started."""
    return _is_our_pid(pid, b"emporia-relay", b"emporia.relay_server")


def _start_relay(env: dict[str, str] | None = None, url: str | None = None, say=print) -> int:
    pid_file, log = _relay_files()
    url = url or relay_url()
    exe = Path(sys.executable).with_name("emporia-relay")
    argv = [str(exe)] if exe.exists() else [sys.executable, "-m", "emporia.relay_server"]
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as out:
        proc = subprocess.Popen(argv, cwd=REPO, stdout=out, stderr=out, env=env,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    pid_file.write_text(str(proc.pid))
    for _ in range(80):
        if health(timeout=1, url=url):
            pid_file.with_suffix(".url").write_text(_same_host(url))   # where it answered (P285)
            say(f"relay: started at {url} (pid {proc.pid}, log {log})")
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    say(f"relay: did not start — see `emporia relay logs` ({log})")
    return 1


def _same_host(url: str) -> str:
    """A loopback URL in one spelling, so localhost and 127.0.0.1 compare equal."""
    import urllib.parse
    u = urllib.parse.urlparse(url.strip().rstrip("/"))
    host = "127.0.0.1" if (u.hostname or "").lower() in ("localhost", "127.0.0.1", "::1") else (u.hostname or "")
    return f"{u.scheme}://{host}:{u.port or (443 if u.scheme == 'https' else 80)}"


def _dotenv_names(path: str) -> set[str]:
    """The names a .env sets (a Hermes profile's read through Hermes, catalog rule 11)."""
    from emporia import profile_env
    return set(profile_env.read(Path(path))) if path else set()


def ensure_local_relay(url: str) -> str:
    """P285: bring back the relay this machine runs, after a container or machine restart.

    Called by each agent's bridge when its gateway starts. Starts the relay only when the
    agent's relay is on this machine, does not answer, and `emporia relay start` was used
    here and not followed by `emporia relay stop` (its pid file is still there) — so a relay
    the operator stopped stays stopped, and one run elsewhere is never touched. Under a lock,
    so several agents coming up together start it once. The relay gets a clean environment:
    nothing of the agent's (its .env holds the agent's keys); it reads its own profile's .env,
    as when started from a shell. Returns what it did, for the bridge's log."""
    import urllib.parse
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host not in ("localhost", "127.0.0.1", "::1"):
        return "not on this machine"
    pid_file, _ = _relay_files()
    if health(timeout=2, url=url):
        return "up"
    if not pid_file.exists():
        return "not started here (run `emporia relay start` once)"
    try:
        served = pid_file.with_suffix(".url").read_text().strip()
    except OSError:
        served = ""
    if served != _same_host(url):            # another relay on this machine (a test's, another port)
        return "not the relay `emporia relay start` runs here"
    import fcntl
    with open(pid_file.with_suffix(".lock"), "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        if health(timeout=2, url=url):
            return "up"
        try:
            pid = int(pid_file.read_text())
        except (OSError, ValueError):
            return "stopped (no pid file)"
        if _is_relay_pid(pid):                 # running but not answering yet: give it time
            for _ in range(40):
                if health(timeout=1, url=url):
                    return "up"
                time.sleep(0.25)
            return f"running (pid {pid}) but not answering — see `emporia relay logs`"
        handed = os.environ.get("EMPORIA_HANDED_NAMES")
        if handed is not None:
            # Catalog rule 11: the bridge reads no .env; what it holds of the agent's is what
            # the plugin handed it (adapter._profile_values), and it names them.
            agent = {n for n in handed.split(",") if n}
        else:
            agent = _dotenv_names(os.environ.get("EMPORIA_DOTENV", ""))
        env = {k: v for k, v in os.environ.items() if not k.startswith("EMPORIA_") and k not in agent}
        quiet = lambda *_: None   # noqa: E731 — the bridge's stdout is its event stream
        return "started" if _start_relay(env, url, say=quiet) == 0 else "did not start — see `emporia relay logs`"


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
        # P214: short — the URL and what matters; the rest shows when highlighted.
        state = _relay_brief(up[u]) if up[u] else "not running"
        if up[u] and u == LOCAL_RELAY and str(up[u].get("version")) != __version__:
            state += " — older code; restart it: emporia relay restart"
        desc = (f"{u}  ({'current, ' if u == current else ''}{state}"
                + (f"; used by {', '.join(who)}" if who else "") + ")")
        options.append((f"use:{u}", desc))
        if u == current or (default is None and up[u]):
            default = len(options) - 1
    if not up.get(LOCAL_RELAY):
        options.append(("local", "Start a relay on this machine"))
    options.append(("remote", "Join someone else's relay (I have its URL)"))
    options.append(("host", "Host a relay for other machines (Docker or a system service)"))
    options.append(("later", f"Decide later (keeps {current or LOCAL_RELAY})"))
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
    wizard_commit()        # P213: it starts a relay; no going back past that
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
            print("  Skipped — later: emporia setup relay, then emporia relay restart")
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


@wizard
def cmd_relay_host(args: argparse.Namespace) -> int:
    """A relay for agents on other machines, with https in front (P183): in
    Docker, or as a system service on a server kept for it. Only a person runs
    it: it opens a relay to the internet. Its settings live in its own folder,
    never in a Hermes profile."""
    import shutil

    from emporia import relay_host as rh

    if not _persons_terminal():
        print("relay host: only a person runs this, in their own terminal — it opens a relay to other machines.")
        return 1
    ways, why = rh.ways_to_run(rh.in_container(), bool(shutil.which("docker")),
                               Path("/run/systemd/system").is_dir())
    run = args.run
    heading("Host a relay", "For agents on other machines — behind https, in Docker or as a system service.")
    if not run and len(ways) == 1:
        run = ways[0]
        print(f"  {why}")
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
    heading("Its address", "Others reach it at https://<domain>. Point the domain's DNS (an A record) at this "
            "machine first; ports 80 and 443 must be open for the certificate. Leave it blank if you put your own "
            "proxy in front.")
    domain = (args.domain or prompt("Domain", "")).strip().lower()
    if domain and not rh.valid_domain(domain):
        print(f"relay host: {domain!r} is not a domain name, like relay.example.com")
        return 1
    where = "in Docker beside it" if run == "docker" else "on this server, as its own user"
    heading("Its operator", f"The agent that sees its settings and activity. A Hermes agent can run it {where}: "
            "name that agent's profile, or leave it blank.")
    admin_name = (args.admin if args.admin is not None else prompt("Agent profile to run it", "")).strip()
    admin = profile_dir(admin_name) if admin_name else None
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
    heading("Who may use it")
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
    # P291: what it runs beyond its core — every module optional, each asked its own questions
    existing = installer().harvest_env(folder) if (folder / ".env").exists() else {}
    asked = _ask_modules(args, existing, ask_money=True)
    if asked is None:
        return 1
    module_updates, modules = asked
    updates.update(module_updates)
    payments = "money" in modules
    if admin:
        updates["EMPORIA_AGENT_ID"] = installer().harvest_env(admin).get("EMPORIA_AGENT_ID") or admin_name
    env_path = folder / ".env"
    wizard_commit()        # P213: from here it writes the relay's settings
    heading("Saving")
    merge_env_file(env_path, updates)
    env_path.chmod(0o600)
    for path in (*written, env_path):
        print(f"  wrote {path}")
    # P208: its keys are apart from Hermes only if Hermes's user cannot get round that.
    if ways := _reachable_anyway(run == "docker"):
        print(f"  ! Hermes runs here as a user for whom {'; '.join(ways)} — its agents could reach this relay's keys "
              f"(identity, operator key, Stripe key). Fine on testnet; before real money take that away, or run the "
              f"relay on its own server.")
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


def _modules_line(mods: dict) -> str:
    from emporia import relay_modules
    return relay_modules.describe(mods)


_GAMIFY_PARTS_ABOUT = [
    ("sheets", "sheets — character sheets: Craft per game, Trade, Trust, levels and class"),
    ("bugs", "bugs   — points for bugs agents find in the relay, logged in the transparency log"),
    ("quests", "quests — welcome quests leading to bug reports and improvements: XP and badges, no standing"),
]


def _module_items(items: list[str]) -> dict[str, set[str]] | None:
    """`gamify:sheets,bugs log` → {"gamify": {"sheets", "bugs"}, "log": set()}; None (said why) if
    a module or part does not exist. Money's parts come from its own setup, not from here."""
    from emporia import relay_modules as rm
    out: dict[str, set[str]] = {}
    for item in items:
        name, _, raw = item.strip().lower().partition(":")
        if name not in rm.BY_NAME:
            print(f"  no module called {name!r} — the modules: {', '.join(rm.BY_NAME)}")
            return None
        parts = {p.strip() for p in raw.split(",") if p.strip()}
        if parts and name == "money":
            print("  money's parts are its ways to pay, set up by their own commands: "
                  "emporia setup relay (x402, escrow, Tempo) · emporia setup stripe (cards)")
            return None
        if bad := sorted(parts - set(rm.BY_NAME[name].parts)):
            print(f"  {name} has no part called {', '.join(bad)} — its parts: {', '.join(rm.BY_NAME[name].parts) or 'none'}")
            return None
        out[name] = out.get(name, set()) | parts
    return out


def _ask_modules(args: argparse.Namespace, env: dict[str, str], *, ask_money: bool,
                 want: set[str] | None = None,
                 parts: dict[str, set[str]] | None = None) -> tuple[dict[str, str], set[str]] | None:
    """P291: which modules a relay runs — the tick-list (or `want`, given), then
    each module's own questions. `parts`: parts named on the command line, which
    are not asked again. Returns (settings to write, the modules wanted), or
    None when an answer was refused. `ask_money` False: the caller runs money's
    own setup (`setup relay`, which also makes the operator key)."""
    import re

    from emporia import relay_modules as rm
    from emporia.identity import normalize_peer_url

    now = rm.chosen(env)
    parts = parts or {}
    given = getattr(args, "modules", "") or ""
    if want is not None:
        want, given = set(want), given or "command"
    elif given:
        named = _module_items([m for m in given.split(",") if m.strip()])
        if named is None:
            return None
        want = set(named)
    else:
        ticked = set(now)
        if getattr(args, "transparency_log", "") in ("on", "off"):
            (ticked.add if args.transparency_log == "on" else ticked.discard)("log")
        if getattr(args, "fee_receiver", ""):
            ticked.add("money")
        heading("Modules", "What this relay runs beyond its core (agents, games, listings, rooms, agoras, DMs). "
                "Each is optional; change them any time with `emporia relay modules`.")
        want = set(choose_many("Tick what it runs:", [(m.name, f"{m.name:<10} {m.about}") for m in rm.MODULES],
                               ticked))
    out: dict[str, str] = {}

    # gamify first: bug quests decide whether the log is needed
    gamify: set[str] = set()
    if "gamify" in want:
        gamify = set(parts.get("gamify") or now.get("gamify") or ({"sheets", "bugs"} if "log" in want else {"sheets"}))
        if _TTY and not given:
            gamify = set(choose_many("Gamify — which parts:", _GAMIFY_PARTS_ABOUT, gamify))
        if "bugs" in gamify and "log" not in want:
            if not _TTY:
                print("  bug quests need the log module: add it too — emporia relay modules add log gamify:bugs")
                return None
            if ask("Bug quests need the transparency log (each report and verdict is an entry in it). "
                   "Turn the log on too?", True):
                want.add("log")
            else:
                gamify.discard("bugs")
                print("  bug quests left off")
        if not gamify:
            want.discard("gamify")
    out["EMPORIA_GAMIFY"] = ",".join(p for p, _ in _GAMIFY_PARTS_ABOUT if p in gamify)

    if "log" in want and "log" not in now and not given:
        print(f"  The log: {_LOG_ABOUT}")
    out["EMPORIA_TRANSPARENCY_LOG"] = "on" if "log" in want else "off"

    links = [normalize_peer_url(u) for u in env.get("FEDERATED_RELAYS", "").split(",") if u.strip()]
    if "federation" in want:
        if not links and _TTY and not given:
            for raw in prompt("Relays to link (https URLs, comma-separated; blank: none yet)", "").split(","):
                if raw.strip() and (url := _check_relay_url(normalize_peer_url(raw.strip()))):
                    links.append(normalize_peer_url(url))
        if not links:
            print("  No relay linked, so federation stays off until one is: emporia relay link URL")
            want.discard("federation")
        else:
            dms = ("dms" in parts["federation"]) if "federation" in parts else ask(
                "Sealed DMs with relays linked both ways (each agent still opts in)?",
                "dms" in now.get("federation", set()))
            out["EMPORIA_FEDERATED_DMS"] = "1" if dms else "0"
            out["FEDERATED_RELAYS"] = ",".join(dict.fromkeys(links))
    elif "federation" in now:
        out["FEDERATED_RELAYS"], out["EMPORIA_FEDERATED_DMS"] = "", "0"
        print("  Federation off: its links are removed; linked relays drop this one's listings at their next sync.")

    if "money" in want and "money" not in now and ask_money:
        fee = getattr(args, "fee_receiver", "") or prompt(
            "Testnet USDC and escrow (Base Sepolia). Address that receives your fee (a wallet you control, ideally a Safe)")
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", fee or ""):
            print("  Not an address (0x and 40 hex) — money left off for now; `emporia relay modules` adds it.")
            want.discard("money")
        else:
            out.update(_BASE_SEPOLIA)
            out["EMPORIA_ESCROW_FEE_RECEIVER"] = fee
    elif "money" in now and "money" not in want:
        out.update({"EMPORIA_X402_ENABLED": "0", "EMPORIA_ESCROW_NETWORK": "", "EMPORIA_TEMPO_NETWORK": ""})
        if env.get("STRIPE_SECRET_KEY"):
            print("  Cards stay on while its Stripe key is set: emporia relay config unset STRIPE_SECRET_KEY")
    # only what changes; a module turned off keeps its data, only unmounted
    return {k: v for k, v in out.items() if str(env.get(k, "")) != v and (v or k in env)}, want


def cmd_relay_modules(args: argparse.Namespace) -> int:
    """P291: `emporia relay modules` — what this relay runs beyond its core, and
    a change to it:

        emporia relay modules                       what runs; in a terminal, a tick-list
        emporia relay modules add log gamify:bugs   add modules, or parts of one
        emporia relay modules remove gamify:bugs    remove a part, or a whole module
        emporia relay modules --modules log,gamify  exactly these, for scripts

    Writes its .env after showing every change; the relay applies it at its
    next start, and refuses to start, saying why, if the modules do not hold
    together."""
    from emporia import relay_modules as rm

    env_path = _relay_env_path(args)
    if env_path is None:
        print("relay modules: run it from the relay profile's emporia checkout, or give --dir for a hosted relay")
        return 1
    hosted = bool(getattr(args, "dir", ""))
    env = installer().harvest_env(env_path.parent) if env_path.exists() else {}
    h = None if hosted else health(timeout=3)
    live = (h or {}).get("relay_modules")
    heading("Relay modules")
    print(f"  Runs now:   {_modules_line(live) or 'the core only'}" if live is not None else
          f"  Set up:     {_modules_line(rm.chosen(env)) or 'the core only'}  (the relay is not answering)")
    verb = (args.url or "").strip().lower()
    want, parts = None, None
    if verb:
        if verb not in ("add", "remove") or not args.names:
            print("relay modules: add NAME[:PART,…]… or remove NAME[:PART,…]… — e.g. emporia relay modules add "
                  "log gamify:sheets,bugs")
            return 1
        named = _module_items(args.names)
        if named is None:
            return 1
        now = rm.chosen(env)
        want, parts = set(now), {}
        for name, ps in named.items():
            if verb == "add":
                want.add(name)
                if ps:
                    parts[name] = now.get(name, set()) | ps
            elif ps:                                    # remove some parts; the module goes with its last
                left = now.get(name, set()) - ps
                if left or not rm.BY_NAME[name].parts or name == "federation":
                    parts[name] = left
                else:
                    want.discard(name)
            else:
                want.discard(name)
    elif not _TTY and not args.modules:
        for m in rm.MODULES:
            print(f"  {m.name:<10} {m.about} — {m.change}")
        print("  Change: emporia relay modules add|remove NAME[:PART,…]")
        return 0
    asked = _ask_modules(args, env, ask_money=hosted, want=want, parts=parts)
    if asked is None:
        return 1
    updates, want = asked
    money_here = not hosted and "money" in want and "money" not in rm.chosen(env)
    if not updates and not money_here:
        print("  Nothing to change.")
        return 0
    if updates:
        print(f"  Will set in {env_path}:")
        for k, v in updates.items():
            print(f"    {k}={v}" if v else f"    {k} (cleared)")
        if not args.yes and not ask("Write these?", True):
            return 1
        wizard_commit()
        try:
            merge_env_file(env_path, updates)
        except PermissionError:
            print(f"relay modules: {env_path} belongs to root (a relay run as a service) — set these there with sudo")
            return 1
    if money_here:
        print("  Money: its own setup, next (networks, fee address, operator key).")
        rc = cmd_setup_relay(argparse.Namespace(preset="base-sepolia", with_networks="", fee_receiver=args.fee_receiver,
                                                yes=args.yes))
        if rc:
            return rc
    print("  Restart the relay to apply it: emporia relay restart"
          + ("  (or `docker compose restart relay` in its folder)" if hosted else ""))
    return 0


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
        print("  Restart the relay to apply it: emporia relay restart"
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
            from emporia.addresses import host_of, same_host
            back = any(same_host(host_of(x.get("url", "")), host_of(me_url)) for x in theirs)
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
             "\n  Restart it once so it also stops pulling from linked relays: emporia relay restart"))
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


def cmd_delete(args: argparse.Namespace) -> int:
    """P192: `emporia delete NAME KIND ITEM [--thread T]` — remove something NAME
    posted (or, as a moderator, something posted where NAME moderates). Signed
    with NAME's key; the text is erased and a signed record stays."""
    import json as _json
    agents = {p.name: p for p, _ in _agent_profiles(profiles_root())}
    if args.name not in agents:
        print(f"delete: no agent {args.name!r} ({', '.join(agents) or 'none set up'})")
        return 1
    r = _as_agent(agents[args.name], "import json\nfrom emporia import mcp_server as m\n"
                  f"print(json.dumps(m.delete_item({args.kind!r}, {args.item!r}, thread_id={args.thread!r})))")
    try:
        out = _json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(f"delete: {(r.stderr or r.stdout).strip()[-300:]}")
        return 1
    if not out.get("success"):
        print(f"delete: {out.get('detail') or out.get('error')}")
        return 1
    removed = out.get("removed") or out.get("already") or {}
    print(f"  removed {args.kind} {args.item} — as its {removed.get('role', '?')}, signed by {removed.get('by', '?')}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """P287: `emporia stats NAME [--days N] [--json]` — NAME's own figures: earned (by rail)
    and owed, spent, held in escrow, games and trades, relay-started turns, and what its
    Emporia conversations cost in model tokens (from its profile's Hermes state.db), so a
    net figure. Read as the agent, signed with its key; nothing is sent anywhere else."""
    import json as _json
    agents = {p.name: p for p, _ in _agent_profiles(profiles_root())}
    if args.name not in agents:
        print(f"stats: no agent {args.name!r} ({', '.join(agents) or 'none set up'})")
        return 1
    r = _as_agent(agents[args.name], "import json\nfrom emporia import mcp_server as m\n"
                  f"print(json.dumps(m.my_stats({int(args.days)})))", EMPORIA_NO_ARCHIVE="1")
    try:
        out = _json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(f"stats: {(r.stderr or r.stdout).strip()[-300:]}")
        return 1
    if not out.get("success"):
        print(f"stats: {out.get('error')}")
        return 1
    print(_json.dumps({k: v for k, v in out.items() if k != "summary"}, indent=2) if args.json else out["summary"])
    return 0


def cmd_sheet(args: argparse.Namespace) -> int:
    """P288: `emporia sheet NAME [--of AGENT] [--json]` — NAME's sheet on its relay (or
    AGENT's, read as NAME): Craft per game, Trade, Trust, levels and class. Only on a relay
    that runs gamify sheets (EMPORIA_GAMIFY=sheets)."""
    import json as _json
    agents = {p.name: p for p, _ in _agent_profiles(profiles_root())}
    if args.name not in agents:
        print(f"sheet: no agent {args.name!r} ({', '.join(agents) or 'none set up'})")
        return 1
    r = _as_agent(agents[args.name], "import json\nfrom emporia import mcp_server as m\n"
                  f"print(json.dumps(m.character_sheet({(args.of or '')!r})))", EMPORIA_NO_ARCHIVE="1")
    try:
        out = _json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(f"sheet: {(r.stderr or r.stdout).strip()[-300:]}")
        return 1
    if not out.get("success"):
        print(f"sheet: {out.get('detail') or out.get('error')}")
        return 1
    from emporia import progression
    print(_json.dumps({k: v for k, v in out.items() if k != "success"}, indent=2) if args.json
          else progression.render(out))
    return 0


def cmd_bugs(args: argparse.Namespace) -> int:
    """P289: `emporia bugs NAME` — the bug reports on NAME's relay and NAME's points.
    As the relay's operator, a person (refused from an agent's tools):
    `emporia bugs NAME verdict ID accepted --severity high --fix COMMIT` (or `duplicate
    --of ID`, or `invalid`), `emporia bugs NAME incident "SUMMARY" [--affected …]
    [--report ID]`, `emporia bugs NAME fixed INCIDENT_ID`. Each is an entry in the relay's
    transparency log."""
    import json as _json
    agents = {p.name: p for p, _ in _agent_profiles(profiles_root())}
    if args.name not in agents:
        print(f"bugs: no agent {args.name!r} ({', '.join(agents) or 'none set up'})")
        return 1
    act = args.act or "list"
    if act == "list":
        code = "import json\nfrom emporia import mcp_server as m\nprint(json.dumps(m.bug_reports()))"
    else:
        if act == "verdict":
            if len(args.rest) < 2:
                print("bugs: verdict ID accepted|duplicate|invalid")
                return 1
            path, body = f"/bugs/{args.rest[0]}/verdict", {
                "verdict": args.rest[1], "severity": args.severity or "", "fix_commit": args.fix or "",
                "duplicate_of": getattr(args, "of", None) or ""}
        elif act == "incident":
            if not args.rest:
                print('bugs: incident "what happened"')
                return 1
            path, body = "/security/incidents", {"summary": " ".join(args.rest), "affected": args.affected or "",
                                                 "report_id": args.report or ""}
        elif act == "fixed":
            if not args.rest:
                print("bugs: fixed INCIDENT_ID")
                return 1
            path, body = f"/security/incidents/{args.rest[0]}/fixed", {}
        else:
            print("bugs: list, verdict, incident or fixed")
            return 1
        if not _persons_terminal():
            # Operator section: a verdict gives points and an incident is on the record for good —
            # the person's signed decision, in their own terminal (the key guard refuses it too).
            print(f"bugs: only a person gives a {act}, in their own terminal — never through an agent.")
            return 1
        _, agent_id, _env = _agent(args.name)
        if act == "verdict":
            # Security review pass 2 (P1): signed by the operator's verdict key, a person's key
            # no agent holds; sent with the operator's sign-in, as before.
            from emporia import wire
            rid = args.rest[0]
            return _send_verdict(agents[args.name], agent_id, path.lstrip("/"), body, "bugs",
                                 lambda relay, key, at: wire.signed_bug_verdict_payload(
                                     rid, body["verdict"], body["severity"], body["fix_commit"],
                                     body["duplicate_of"], relay, key, at))
        # An incident: the operator's sign-in, the way the console signs — a person's command.
        status, d = _signed_get(agent_id, _key_file(agents[args.name], agent_id, "identity").parent,
                                path.lstrip("/"), "POST", body)
        if status != 200:
            print(f"bugs: {(d or {}).get('detail', d) if isinstance(d, dict) else d}")
            return 1
        print(_json.dumps(d, indent=2))
        return 0
    r = _as_agent(agents[args.name], code, EMPORIA_NO_ARCHIVE="1")
    try:
        out = _json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(f"bugs: {(r.stderr or r.stdout).strip()[-300:]}")
        return 1
    if not out.get("success"):
        print(f"bugs: {out.get('detail') or out.get('error')}")
        return 1
    if args.json or act != "list":
        print(_json.dumps({k: v for k, v in out.items() if k != "success"}, indent=2))
        return 0
    pts = out.get("points") or {}
    print(f"{args.name}: {pts.get('points', 0)} points · {pts.get('accepted', 0)} accepted, "
          f"{pts.get('open', 0)} open, {pts.get('duplicate', 0)} duplicate, {pts.get('invalid', 0)} invalid")
    for b in out.get("reports") or []:
        what = "sealed" if b["sealed"] and not b["revealed"] else (b.get("title") or "")
        print(f"  {b['report_id']}  {b['status']:<9} {b.get('severity') or b['severity_claimed']:<8} "
              f"{b['points']:>3} pts  {b['reporter_id']:<12} {b['commit'][:10]}  {what}")
    return 0


def cmd_ideas(args: argparse.Namespace) -> int:
    """Launch additions: `emporia ideas NAME` — the relay operator's verdicts on posts in the ideas
    topic. As the relay's operator, a person (no agent tool does this):
    `emporia ideas NAME adopt POST --ref COMMIT_OR_ROADMAP_ITEM` (the first post of the
    idea: it earns the improvement points), `emporia ideas NAME noted POST`, `emporia ideas
    NAME duplicate POST --of EARLIER_POST`. Each verdict is signed with NAME's key and is an
    entry in the relay's transparency log; it is final."""
    import json as _json
    agents = {p.name: p for p, _ in _agent_profiles(profiles_root())}
    if args.name not in agents:
        print(f"ideas: no agent {args.name!r} ({', '.join(agents) or 'none set up'})")
        return 1
    act = args.act or "list"
    if act == "list":
        code = "import json\nfrom emporia import mcp_server as m\nprint(json.dumps(m._ideas_judged()))"
    else:
        if not args.rest:
            print(f"ideas: {act} POST_ID")
            return 1
        if act == "adopt" and not args.ref:
            print("ideas: adopt POST_ID --ref COMMIT_OR_ROADMAP_ITEM (what the idea led to)")
            return 1
        if act == "duplicate" and not args.of:
            print("ideas: duplicate POST_ID --of EARLIER_POST_ID")
            return 1
        verdict = {"adopt": "adopted", "noted": "noted", "duplicate": "duplicate"}[act]
        if not _persons_terminal():
            # As a bug verdict: points on the board and an entry in the log for good — the
            # person's signed decision, in their own terminal (the key guard refuses it too).
            print(f"ideas: only a person gives a verdict ({act}), in their own terminal — never through an agent.")
            return 1
        return _idea_verdict(agents[args.name], args.rest[0], verdict, args.ref or "", args.of or "")
    r = _as_agent(agents[args.name], code, EMPORIA_NO_ARCHIVE="1")
    try:
        out = _json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(f"ideas: {(r.stderr or r.stdout).strip()[-300:]}")
        return 1
    if not out.get("success"):
        print(f"ideas: {out.get('detail') or out.get('error')}")
        return 1
    if args.json or act != "list":
        print(_json.dumps({k: v for k, v in out.items() if k != "success"}, indent=2))
        return 0
    print(f"ideas topic `{out.get('topic')}` · {out.get('points_per_idea')} points per adopted idea")
    for v in out.get("verdicts") or []:
        what = v.get("ref") or (f"duplicate of {v['duplicate_of']}" if v.get("duplicate_of") else "")
        print(f"  {v['post_id']}  {v['verdict']:<9} {v['points']:>3} pts  {v['author_id']:<12} "
              f"{(v.get('title') or '')[:40]:<40}  {what}")
    return 0


def _idea_verdict(profile: Path, post_id: str, verdict: str, ref: str, duplicate_of: str) -> int:
    """An idea verdict (`emporia:idea-verdict:v2`), signed by the operator's verdict key — a
    person's key, in the signer's folder, never the operator agent's — and sent with the
    operator's sign-in, as a bug verdict goes. No MCP tool reaches `/ideas/{id}/verdict`."""
    from emporia import wire
    from emporia.wire import path_id
    _, agent_id, _env = _agent(profile.name)
    ref = ref.strip() if verdict == "adopted" else ""
    duplicate_of = duplicate_of.strip() if verdict == "duplicate" else ""
    return _send_verdict(profile, agent_id, f"ideas/{path_id(post_id)}/verdict",
                         {"verdict": verdict, "ref": ref, "duplicate_of": duplicate_of}, "ideas",
                         lambda relay, key, at: wire.signed_idea_verdict_payload(
                             post_id, verdict, ref, duplicate_of, relay, key, at))


def _send_verdict(profile: Path, agent_id: str, path: str, body: dict, label: str, build) -> int:
    """Security review pass 2 (P1): sign a verdict with this person's verdict key and send it as
    the operator. `build(relay_id, verdict_key, at)` is the payload; the relay checks it against
    the verdict key it has registered (`EMPORIA_OPERATOR_VERDICT_KEY`), and refuses anything else."""
    import json as _json
    from datetime import UTC, datetime

    from emporia import operator_verdicts as ov
    key_dir = _key_file(profile, agent_id, "identity").parent
    status, who = _signed_get(agent_id, key_dir, "operator/whoami")
    if status != 200 or not isinstance(who, dict):
        print(f"{label}: {_relay_refusal(status, who)}")
        return 1
    if not who.get("is_operator"):
        print(f"{label}: {agent_id} is not this relay's operator — a verdict is its operator's")
        return 1
    try:
        mine = ov.public_hex()
    except ov.VerdictKeyError as e:
        print(f"{label}: {e}")
        return 1
    if not mine:
        print(f"{label}: no verdict key here ({ov.key_path()}). A verdict is signed by the operator's verdict key, "
              f"a person's key no agent holds: make one with `emporia operator {profile.name} verdict-key new`, then "
              "register it with the relay")
        return 1
    theirs = who.get("verdict_key") or ""
    if theirs != mine:
        print(f"{label}: the relay takes verdicts from " + (f"the verdict key {theirs[:16]}…" if theirs else "no key yet")
              + f", and yours is {mine[:16]}… — in the relay's .env set {ov.ENV}={mine} and restart the relay "
              f"(`emporia operator {profile.name} verdict-key` shows both)")
        return 1
    at = datetime.now(UTC).isoformat()
    try:
        signature = ov.sign(build(str(who.get("relay_id") or ""), mine, at))
    except ov.VerdictKeyError as e:
        print(f"{label}: {e}")
        return 1
    status, d = _signed_get(agent_id, key_dir, path, "POST",
                            {**body, "at": at, "signature": signature, "verdict_key": mine})
    if status != 200:
        print(f"{label}: {(d or {}).get('detail', d) if isinstance(d, dict) else d}")
        return 1
    print(_json.dumps(d, indent=2))
    return 0


def _verdict_key_cmd(profile: Path, agent_id: str, what: str) -> int:
    """`emporia operator NAME verdict-key [new|rotate]`: the operator's verdict key — shown, made,
    or replaced — and whether NAME's relay has it registered. A person's command: the key signs
    the verdicts that award points, so no agent makes, reads or uses it."""
    from emporia import operator_verdicts as ov
    if what not in ("", "show", "new", "rotate"):
        print(f"operator verdict-key: emporia operator {profile.name} verdict-key [new|rotate]")
        return 2
    if not _persons_terminal():
        print("operator verdict-key: only a person makes or uses the operator's verdict key, in their own terminal "
              "— never through an agent.")
        return 1
    try:
        if what in ("new", "rotate"):
            pub, retired = ov.new(rotate=what == "rotate")
            print(f"  A new verdict key, kept at {ov.key_path()} (0600; the signer's folder, no agent profile).")
            if retired:
                print(f"  The old one is kept beside it, as {retired.name}; the relay stops taking it once the new "
                      "public key is in its .env and it restarts.")
        pub = ov.public_hex()
    except ov.VerdictKeyError as e:
        print(f"operator verdict-key: {e}")
        return 1
    print(f"  Your verdict key: {pub or 'none here — make one: emporia operator ' + profile.name + ' verdict-key new'}")
    status, who = _signed_get(agent_id, _key_file(profile, agent_id, "identity").parent, "operator/whoami")
    if status != 200 or not isinstance(who, dict) or not who.get("is_operator"):
        print("  The relay: " + (_relay_refusal(status, who) if status != 200 or not isinstance(who, dict)
                                 else f"{agent_id} is not its operator"))
        return 0 if what else 1
    theirs = who.get("verdict_key") or ""
    if pub and theirs == pub:
        print("  The relay takes verdicts from it: registered.")
        return 0
    print("  The relay takes verdicts from " + (f"another key ({theirs[:16]}…)" if theirs else "no key yet")
          + (f" ({who['verdict_key_problem']})" if who.get("verdict_key_problem") and not theirs else ""))
    if pub:
        print(f"  Register yours, on the relay's machine: {ov.ENV}={pub} in its .env (or `emporia relay config set "
              f"{ov.ENV} {pub}`), then restart the relay.")
    return 0 if what else 1


def cmd_archive(args: argparse.Namespace) -> int:
    """P194: `emporia archive NAME` — refresh NAME's own archive of everything it
    is part of on its relay (DMs, rooms, its posts and listings, its sessions);
    `--read dm|room ID` shows one thread, sealed messages opened here with NAME's
    key. Reading is for a person, in their own terminal."""
    import json as _json
    agents = {p.name: p for p, _ in _agent_profiles(profiles_root())}
    if args.name not in agents:
        print(f"archive: no agent {args.name!r} ({', '.join(agents) or 'none set up'})")
        return 1
    profile = agents[args.name]
    if not args.read:
        r = _as_agent(profile, "import json\nfrom emporia import mcp_server as m\nprint(json.dumps(m.archive_now()))",
                      EMPORIA_NO_ARCHIVE="1")
        try:
            out = _json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            print(f"archive: {(r.stderr or r.stdout).strip()[-300:]}")
            return 1
        saved = out.get("saved") or {}
        print(f"  {args.name}'s archive: {out.get('archive')}")
        print("  new: " + (", ".join(f"{v} {k.replace('_', ' ')}" for k, v in saved.items() if v) or "nothing"))
        if not out.get("success"):
            print(f"  stopped early: {out.get('error')}")
        return 0 if out.get("success") else 1
    if not _persons_terminal():
        print("archive --read: only a person reads an agent's archive, in their own terminal.")
        return 1
    kind, item = args.read
    if kind not in ("dm", "room"):
        print("archive --read: dm THREAD_ID or room ROOM_ID")
        return 1
    code = f"""import json
from pathlib import Path
from emporia import message_read, mcp_server as m
from emporia.identity import KEY_DIR, encryption_private_keys
hits = list((KEY_DIR / "archive").glob("*/{'dms' if kind == 'dm' else 'rooms'}/{item}.jsonl"))
if not hits:
    print(json.dumps({{"error": "not in the archive — run: emporia archive {args.name}"}}))
else:
    msgs = [json.loads(x) for x in hits[0].read_text().splitlines() if x.strip()]
    priv = encryption_private_keys(m.AGENT_ID)
    me = next((x.get("sender_id") for x in msgs if x.get("sender_id", "").startswith(m.AGENT_ID + "@")), m.AGENT_ID)
    message_read.annotate_messages(msgs, kind={kind!r}, scope_id={item!r}, viewer_agent_id=me, private_key=priv,
                                   pubkeys={{}}, verify=lambda msg, pk: None)
    print(json.dumps([[x.get("created_at", "")[:16], x.get("sender_id"), x.get("content"),
                       x.get("deleted_at") is not None] for x in msgs]))"""
    r = _as_agent(profile, code, EMPORIA_NO_ARCHIVE="1")
    try:
        rows = _json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(f"archive: {(r.stderr or r.stdout).strip()[-300:]}")
        return 1
    if isinstance(rows, dict):
        print(f"archive: {rows['error']}")
        return 1
    for when, who, content, removed in rows:
        print(f"  {when}  {who}: " + ("(removed)" if removed else content))
    return 0


def _signer_url(args: argparse.Namespace) -> str:
    from emporia import signer
    if args.socket:
        os.environ["EMPORIA_SIGNER_SOCKET"] = args.socket
    return args.url or os.getenv("EMPORIA_SIGNER_URL", "") or signer.default_url()


def cmd_signer(args: argparse.Namespace) -> int:
    """P203: `emporia signer` — in a terminal with nothing after it, a menu;
    or start|stop|logs|serve|status|add|remove|policy|pending|approve|deny.

    serve runs the signer (as its own user, or in its own container — see the
    docs). add moves an agent's wallet keys and limits into it and points the
    agent at it; policy changes an agent's limits there; pending/approve/deny
    answer payments above the approval threshold. All but status are a person's,
    on the signer's machine, as the signer's user (they read its folder)."""
    from emporia import signer, signer_client
    if args.action == "serve":
        if args.dir:
            os.environ["EMPORIA_SIGNER_DIR"] = args.dir
        url = _signer_url(args)
        print(f"emporia signer: keys and limits in {signer.signer_dir()}; agents reach it at "
              + (f"http://127.0.0.1:{args.port}" if args.port else url))
        signer.serve(socket=args.socket, port=args.port)
        return 0
    if args.dir:
        os.environ["EMPORIA_SIGNER_DIR"] = args.dir
    url = _signer_url(args)
    if args.action in ("start", "stop", "logs"):
        return _signer_daemon(args, url)
    if args.action == "host":
        return _signer_host(args)
    if args.action in ("audit", "log"):
        return _signer_audit(args.name)
    if args.action == "alerts":
        return _signer_alerts(args)
    if args.action == "remove":
        return _signer_remove(args)
    if args.action == "backend":
        return _signer_backend(args)
    if args.action == "backup":
        return _signer_backup(args)
    if args.action == "restore":
        return _signer_restore(args)
    if args.action == "status":
        try:
            h = signer_client.Client(url, "", "").health()
        except Exception as e:
            print(f"signer: not reachable at {url} — {e}")
            return 1
        apart = h.get("uid") != os.getuid()
        ways_round = _reachable_anyway(bool(h.get("in_container"))) if apart else []
        print(f"signer: running at {url}, as uid {h.get('uid')}" + (
            " — apart from this user, BUT " + "; ".join(ways_round) + ". Close that for it to be a wall."
            if ways_round else " — apart from this user" if apart else
            " — PRACTICE MODE: the same user as the agents, so it adds no protection against a rogue agent "
            "(it can read the signer's folder). Testnet only; apart from them: emporia signer host"))
        try:
            lines = _backends_lines()
        except OSError:
            lines = []
        if len(lines) > 1:
            print("Back ends (emporia signer backend):")
            for line in lines:
                print(line)
        return 0
    if args.action in ("pending", "approve", "deny"):
        admin = signer_client.Admin(url, signer.admin_token())
        if args.action == "pending":
            items = [i for i in admin.intents() if i["status"] == "pending"]
            for i in items:
                print(f"  {i['id']}  {time.strftime('%m-%d %H:%M', time.localtime(i['at']))}  {i['agent']}: "
                      f"{i['what']}")
            print(f"{len(items)} waiting." + (" Approve: emporia signer approve ID · refuse: emporia signer deny ID"
                                               if items else ""))
            return 0
        if not args.name:
            print(f"signer {args.action}: emporia signer {args.action} INTENT_ID")
            return 1
        if args.action == "approve" and not _persons_terminal():
            print("signer approve: only a person approves a payment, in their own terminal.")
            return 1
        try:
            item = admin.decide(args.name, args.action == "approve")
        except RuntimeError as e:
            print(f"signer {args.action}: {e}")
            return 1
        print(f"  {item['id']} {item['status']} — {item['agent']}: {item['what']}"
              + ("\n  The agent's next try of this payment, within an hour, is paid." if item["status"] == "approved"
                 else ""))
        return 0
    if args.action == "policy":
        return _signer_policy(args)
    if args.action == "add":
        return _signer_add(args, url)
    return _signer_menu(args, url)


def _signer_args(action: str, name: str = "", rest: list | None = None, lock: bool = False) -> argparse.Namespace:
    return argparse.Namespace(action=action, name=name, rest=rest or [], dir="", port=0, url="", socket="", lock=lock,
                              run="", make=False, backend=[], out="", from_="", identity=False, key_source=[])


_ESCALATIONS: list[str] | None = None


def _escalations() -> list[str]:
    """P207: ways this user (the agents') could reach a signer that runs apart
    anyway. Any one of them makes its own container or user no wall. Once per
    command (P209): a failed `sudo -n` is written to the host's auth log."""
    global _ESCALATIONS
    if _ESCALATIONS is not None:
        return list(_ESCALATIONS)
    import shutil
    found = []
    if os.getuid() == 0:
        found.append("the agents run as root")
    for sock in ("/var/run/docker.sock", "/run/docker.sock"):
        if os.path.exists(sock) and os.access(sock, os.R_OK | os.W_OK):
            found.append(f"this user can control Docker ({sock}) — an agent could start a container with the "
                         "signer's volume")
            break
    if shutil.which("sudo"):
        try:
            if subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5).returncode == 0:
                found.append("this user has sudo without a password")
        except (OSError, subprocess.TimeoutExpired):
            pass
    _ESCALATIONS = found
    return list(found)


def _reachable_anyway(in_container: bool) -> list[str]:
    """P208: which of _escalations() undo a separation, for something kept apart
    in a container or not. On Linux any of them does: Docker access is root (a
    container may mount /). On macOS Docker Desktop runs as the user, so its
    containers reach only what the user can — a separate account stays apart;
    something inside Docker Desktop does not."""
    ways = _escalations()
    if sys.platform == "darwin" and not in_container:
        ways = [w for w in ways if "Docker" not in w]
    return ways


def _signer_apart() -> bool:
    """A signer answers, runs as another user than this one, and this user has
    no way around that (P207)."""
    h = _signer_up(_signer_url(_signer_args("")))
    return bool(h) and h.get("uid") != os.getuid() and not _reachable_anyway(bool(h.get("in_container")))


def _signer_up(url: str) -> dict | None:
    from emporia import signer_client

    def look() -> dict | None:
        try:
            return signer_client.Client(url, "", "").health()
        except Exception:
            return None
    return _wiz_lookup(("signer", url), look)


def _signer_menu(args: argparse.Namespace, url: str) -> int:
    """`emporia signer` in a terminal: the signer's state, then what to do."""
    from emporia import signer
    if not _TTY:
        print("signer: start|stop|logs|status|add NAME [--make]|remove NAME|policy NAME|backend|backup NAME|"
              "restore NAME|pending|approve ID|deny ID|serve "
              "— or run `emporia signer` in a terminal to choose")
        return cmd_signer(_signer_args("status"))
    while True:
        up = _signer_up(url)
        held = sorted(p.name for p in (signer.signer_dir() / "agents").glob("*") if (p / "policy.json").exists())
        print(f"\nSigner — {'running' if up else 'not running'} at {url}" + (
            "" if not up else " — practice mode: as you, no protection against a rogue agent (testnet only)"
            if up.get("uid") == os.getuid() else " — apart from the agents") + f"; holds {', '.join(held) or 'no agent'}")
        options = ([("stop", "Stop it")] if up else [
            ("start", "Start it in practice mode (in the background, as you — testnet only)"),
            ("host", "Run it apart from the agents (its own container or system user)")])
        if up:
            options += [("add", "Add an agent — its wallet keys and limits move in")]
            options += [("remove", "Remove an agent — its keys and limits go back to its profile"),
                        ("pending", "Payments waiting for your approval"),
                        ("audit", "What it signed, refused or held back"),
                        ("limits", "An agent's limits (caps, approval threshold, mainnet)")] if held else []
        options += [("done", "Done")]
        what = choose("What would you like to do?", options, len(options) - 1)
        if what == "done":
            return 0
        if what in ("start", "stop"):
            _signer_daemon(_signer_args(what), url)
        elif what == "host":
            _signer_host(_signer_args("host"))
        elif what == "add":
            wallets = [p.name for p, _ in _agent_profiles(profiles_root())
                       if p.name not in held and _has_wallet(p, p.name)]
            if not wallets:
                print("  No agent with a wallet left to add (emporia wallet create NAME).")
                continue
            _signer_add(_signer_args("add", choose("Which agent?", [(n, n) for n in wallets])), url)
        elif what == "remove":
            _signer_remove(_signer_args("remove", choose("Which agent?", [(n, n) for n in held])))
        elif what == "pending":
            _signer_pending_menu(url)
        elif what == "audit":
            _signer_audit()
        elif what == "limits":
            _signer_limits_menu(choose("Which agent?", [(n, n) for n in held]))


def _signer_pending_menu(url: str) -> None:
    from emporia import signer, signer_client
    admin = signer_client.Admin(url, signer.admin_token())
    items = [i for i in admin.intents() if i["status"] == "pending"]
    if not items:
        print("  Nothing waiting.")
    for i in items:
        print(f"\n  {i['agent']}: {i['what']}  ({time.strftime('%m-%d %H:%M', time.localtime(i['at']))})")
        answer = choose("  Approve this payment?", [("skip", "Not now"), ("approve", "Approve"), ("deny", "Refuse")])
        if answer != "skip":
            cmd_signer(_signer_args(answer, i["id"]))


def _signer_limits_menu(name: str) -> None:
    from emporia import signer
    env = signer.policy(name).get("env", {})
    labels = {"EMPORIA_X402_MAX_PAYMENT_CENTS": "Most it pays at once (cents)",
              "EMPORIA_X402_MAX_DAILY_CENTS": "Most it pays in 24 hours (cents)",
              "EMPORIA_PAYMENT_APPROVAL_CENTS": "Ask you above (cents; 0 = always)",
              "EMPORIA_X402_PAY_NETWORKS": "Networks it pays on"}
    from emporia.networks import mainnet_unlocked
    pick = choose(f"{name}'s limits:", [(k, f"{v}: {env.get(k, '—')}") for k, v in labels.items()]
                  + [("mainnet", "Mainnet: " + ("unlocked — lock it" if mainnet_unlocked(env) else "locked — unlock (real money)")),
                     ("back", "Back")], len(labels) + 1)
    if pick == "back":
        return
    if pick == "mainnet":
        _signer_policy(_signer_args("policy", name, ["mainnet"], lock=mainnet_unlocked(env)))
        return
    value = prompt(labels[pick], env.get(pick, ""))
    if value:
        _signer_policy(_signer_args("policy", name, ["set", pick, value]))


def _signer_host(args: argparse.Namespace) -> int:
    """P207: `emporia signer host` — the signer apart from the agents. Hermes in
    Docker: its own container beside Hermes's, sharing only a socket volume.
    Hermes on the host: a system service under its own user. Writes the files and
    shows the commands; runs them when it can and the person says so."""
    import shutil

    from emporia import relay_host as rh
    from emporia import signer_host as sh
    if not _persons_terminal():
        print("signer host: only a person sets up where agents' wallet keys live, in their own terminal.")
        return 1
    in_box, docker, systemd = rh.in_container(), bool(shutil.which("docker")), bool(shutil.which("systemctl"))
    ways = ["docker"] if in_box else [w for w, ok in (("docker", docker), ("service", systemd)) if ok] or [
        "docker", "service"]
    heading("The signer, apart from the agents", "Its own container beside Hermes's, or a system service "
            "under its own user.")
    run = getattr(args, "run", "") or (ways[0] if len(ways) == 1 else choose("How should it run?", [
        ("docker", "In its own container, beside Hermes's (Hermes runs in Docker)"),
        ("service", "As a system service under its own user (Hermes runs on this host, without Docker)")]))
    if in_box and run == "service":
        print("signer host: Hermes runs in a container here — a service would be the container's. Its own "
              "container it is (--run docker).")
        return 1
    folder = Path(args.dir).expanduser() if args.dir else (this_profile() or Path.home()) / "emporia-signer"
    if run == "docker":
        # P211: Hermes's data is wherever its container mounts it (/opt/data in the
        # official image, not always), and on the host a folder or a named volume.
        mount = str(Path(os.getenv("HERMES_HOME") or Path.home() / ".hermes").expanduser().resolve())
        kind, source = sh.data_source(mount) if in_box else ("", "")
        hermes_data = mount if not in_box else source if kind == "bind" else "/path/to/hermes-data"
        written = sh.write_docker(folder, REPO, hermes_data, hermes_in_docker=in_box, hermes_mount=mount,
                                  hermes_volume=source if kind == "volume" else "")
        print(f"Wrote {', '.join(str(p) for p in written)}")
        if in_box:
            print("  Hermes's data: " + (
                f"the Docker volume {source!r}, mounted at {mount} in its container" if kind == "volume" else
                f"{source} on the host, mounted at {mount} — check HERMES_DATA in {folder / '.env'}" if kind == "bind"
                else f"mounted at {mount}; set HERMES_DATA in {folder / '.env'} to where it is on the host"))
        reachable = docker and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
        intro = (f"1. On the host, in this folder (wherever Hermes's data is mounted there) — {folder}:"
                 if not reachable else f"1. In {folder}:")
        steps = [("start", ["docker", "compose", "up", "-d", "--build", "signer"])]
        if _host_steps(steps, "Build and start it now?" if reachable else "", intro):
            out = subprocess.run(steps[0][1], cwd=folder, capture_output=True, text=True)
            print("  started" if out.returncode == 0 else f"  failed:\n{(out.stderr or out.stdout)[-1200:]}")
        if in_box:
            print("\n2. Add to Hermes's own compose file, then recreate its container (docker compose up -d):\n"
                  + "\n".join("    " + ln for ln in sh.hermes_compose_lines().splitlines())
                  + f"\n\n3. Move an agent's wallet in (on the host, in {folder}):\n"
                  "    docker compose run --rm admin add NAME\n"
                  "   then restart the gateway. Approvals and the log, the same way: docker compose run --rm admin "
                  "pending | approve ID | audit\n"
                  f"\nCheck from Hermes: emporia signer status --url {sh.URL}   (it should say: apart from this user)")
            return 0
        print(f"\n2. Move an agent's wallet in (in {folder}):\n"
              f"    docker compose run --rm admin add NAME --url {sh.PORT_URL}\n"
              "   then restart its gateway. Approvals and the log: docker compose run --rm admin pending | approve ID "
              f"| audit\n\nCheck: emporia signer status --url {sh.PORT_URL}")
        if ways_round := _escalations():
            print(f"\n  ! Not a wall yet: Hermes runs here as a user for whom {'; '.join(ways_round)} — its agents "
                  "could reach this container through Docker. It works, as practice. For a wall: on Linux, "
                  "`emporia signer host --run service` (and keep Hermes's user out of the docker group); on macOS or "
                  "Windows, run Hermes itself in Docker, with the signer beside it.")
        return 0
    exe = Path(sys.executable).with_name("emporia")
    written = sh.write_service(folder, exe)
    print(f"Wrote {', '.join(str(p) for p in written)}")
    steps = [("its user", ["sudo", "sh", "-c", f"id -u {sh.SERVICE_USER} >/dev/null 2>&1 || useradd --system "
                                               f"--no-create-home --shell /usr/sbin/nologin {sh.SERVICE_USER}"]),
             ("can it run", ["sudo", "-u", sh.SERVICE_USER, "test", "-x", str(exe)]),
             ("start", ["sudo", "sh", "-c", f"cp {folder / sh.SERVICE} /etc/systemd/system/ && systemctl "
                                            "daemon-reload && systemctl enable --now emporia-signer"])]
    runnable = systemd and bool(shutil.which("sudo"))
    if _host_steps(steps, "Run these now (sudo asks for your password)?" if runnable else "",
                   "1. On this machine, as a person with sudo:"):
        for what, argv in steps:
            out = subprocess.run(argv)
            if out.returncode != 0:
                print(f"signer host: `{' '.join(argv)}` failed" + (
                    " — the signer's user cannot reach this checkout: put it outside a private home folder"
                    if what == "can it run" else ""))
                return 1
    hermes_home = os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))
    print(f"\n2. Move an agent's wallet in, then restart its gateway:\n"
          f"    sudo env HERMES_HOME={hermes_home} {sh.service_env()} {exe} signer add NAME\n"
          f"   Approvals and the log: sudo env {sh.service_env()} {exe} signer pending | approve ID | audit\n"
          f"\nCheck: emporia signer status --url {sh.URL}   (it should say: apart from this user)")
    return 0


def _signer_alerts(args: argparse.Namespace) -> int:
    """P293: `emporia signer alerts [show|telegram|test|off]` — where the signer tells its
    person what it refused and what changed who an agent is. The signer sends them itself,
    never through an agent's chat. Its person's, in their own terminal."""
    import getpass

    from emporia import signer_alerts
    what = args.name or "show"
    if what == "show":
        d = signer_alerts.describe()
        print("  Alerts: " + (", ".join(f"{c} ({v.get('chat_id', '')})" for c, v in d.get("channels", {}).items())
                              if d.get("channels") else "none — emporia signer alerts telegram"))
        return 0
    if not _persons_terminal():
        print("signer alerts: only a person sets where the signer's alerts go, in their own terminal.")
        return 1
    if what == "telegram":
        print("  A Telegram bot of yours: its token from @BotFather, and your chat id (message the bot, then "
              "https://api.telegram.org/bot<token>/getUpdates shows it). Kept in the signer's folder only.")
        token = getpass.getpass("  Bot token (hidden): ").strip() if _TTY else ""
        chat = (args.rest[0] if args.rest else prompt("  Chat id", "")).strip()
        try:
            signer_alerts.set_telegram(token, chat)
        except ValueError as e:
            print(f"signer alerts: {e}")
            return 1
        results = signer_alerts.notify_sync("signer", "test", "alerts are on")
        print("  " + "; ".join(f"{c}: {'sent' if ok else why}" for c, ok, why in results))
        return 0 if all(ok for _, ok, _ in results) else 1
    if what == "test":
        results = signer_alerts.notify_sync("signer", "test", "a test alert")
        print("  " + ("; ".join(f"{c}: {'sent' if ok else why}" for c, ok, why in results) or "no channel set"))
        return 0 if results and all(ok for _, ok, _ in results) else 1
    if what == "off":
        for c in signer_alerts.channels():
            signer_alerts.clear(c)
        print("  Alerts off.")
        return 0
    print("signer alerts: show, telegram, test or off")
    return 1


def _signer_audit(name: str = "") -> int:
    """What the signer signed, refused and held for approval — newest last."""
    import json as _json

    from emporia import signer
    path = signer.signer_dir() / "audit.jsonl"
    rows = [_json.loads(ln) for ln in (path.read_text().splitlines() if path.exists() else []) if ln.strip()]
    rows = [r for r in rows if not name or r.get("agent") == name][-40:]
    for r in rows:
        detail = (f"{r['amount_atomic'] / 1e6:.2f} USDC" if r.get("result") == "signed" and r.get("amount_atomic")
                  else r.get("intent") or r.get("why", ""))
        op = f"{r.get('op', '')}:{r['kind']}" if r.get("kind") else r.get("op", "")
        print(f"  {time.strftime('%m-%d %H:%M:%S', time.localtime(r['at']))}  {r.get('agent', ''):16} "
              f"{op:22} {r.get('result', ''):8} {detail}")
    print(f"{len(rows)} shown ({path})" if rows else "signer: nothing signed or refused yet")
    return 0


def _signer_daemon(args: argparse.Namespace, url: str) -> int:
    """`emporia signer start|stop|logs`: the signer in the background as this
    user — for trying it out. As its own user or container: docs/agent-security-setup.md."""
    from emporia import signer, signer_client
    folder = signer.signer_dir()
    pid_file, log = folder / "signer.pid", folder / "signer.log"
    if args.action == "logs":
        print("".join(log.read_text().splitlines(keepends=True)[-40:]) if log.exists() else "signer: no log yet")
        return 0
    if args.action == "stop":
        try:
            pid = int(pid_file.read_text())
            if not _is_our_pid(pid, b"signer serve"):   # P285: after a reboot the number may be another's
                pid_file.unlink(missing_ok=True)
                raise ProcessLookupError(pid)
            os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            print("signer: none started by `emporia signer start` is running")
            return 1
        pid_file.unlink(missing_ok=True)
        print(f"signer: stopped (pid {pid})")
        return 0
    try:
        signer_client.Client(url, "", "").health()
        print(f"signer: already up at {url}")
        return 0
    except Exception:
        pass
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "emporia.cli", "signer", "serve", "--dir", str(folder)]
    if args.socket:
        argv += ["--socket", args.socket]
    if args.port:
        argv += ["--port", str(args.port)]
    with open(log, "ab") as out:
        proc = subprocess.Popen(argv, cwd=REPO, stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                                start_new_session=True, env={**os.environ, "EMPORIA_NO_DOTENV": "1"})
    pid_file.write_text(str(proc.pid))
    for _ in range(40):
        try:
            signer_client.Client(url, "", "").health()
            print(f"signer: up at {url} (pid {proc.pid}; keys and limits in {folder}) — practice mode: as you, so it "
                  "adds no protection against a rogue agent; for that, emporia signer host\n"
                  "  Next: emporia signer add NAME — then restart the gateway. Undo: emporia signer remove NAME")
            return 0
        except Exception:
            if proc.poll() is not None:
                break
            time.sleep(0.25)
    print(f"signer: did not start — emporia signer logs\n{log.read_text()[-600:] if log.exists() else ''}")
    return 1


def _signer_remove(args: argparse.Namespace) -> int:
    """`emporia signer remove NAME`: the reverse of add — the wallet keys go
    back to the agent's own key files, its limits back to its .env. Testnet only:
    a wallet that may pay real money stays behind the signer."""
    import shutil

    from emporia import agent_wallet, signer
    from emporia.identity import _write_secret_atomically
    from emporia.key_source import unset_env_lines
    name = args.name
    profile = profile_dir(name)
    held = signer.agent_dir(name)
    if not name or not (held / "policy.json").exists() or not (profile / "config.yaml").exists():
        print(f"signer remove: the signer holds no agent {name!r} here")
        return 1
    if not _persons_terminal():
        print("signer remove: only a person moves an agent's keys, in their own terminal.")
        return 1
    if elsewhere := sorted(r for r in signer.routes(name) if r in ("identity", "encryption", "evm", "svm")):
        print(f"signer remove: {name}'s {', '.join(elsewhere)} key(s) are held by a back end (emporia signer backend) "
              "and cannot come out of it; it stays behind the signer.")
        return 1
    env = signer.policy(name).get("env", {})
    from emporia.networks import mainnet_unlocked
    if mainnet_unlocked(env):
        print(f"signer remove: {name} may pay real money; lock that first (emporia signer policy {name} mainnet "
              f"--lock) — a wallet with real money stays behind the signer.")
        return 1
    key_dir = Path(_mcp_env(profile).get("EMPORIA_KEYS_DIR") or profile / "home" / ".hermes" / "keys")
    with agent_wallet.scope(agent_wallet.Scope(env={}, key_dir=held / "keys")):
        sources = {"evm": agent_wallet.wallet_path(name), "svm": agent_wallet.solana_wallet_path(name)}
        spent = agent_wallet._ledger_path(name)
    with agent_wallet.scope(agent_wallet.Scope(env={}, key_dir=key_dir)):
        targets = {"evm": agent_wallet.wallet_path(name), "svm": agent_wallet.solana_wallet_path(name)}
        spent_here = agent_wallet._ledger_path(name)
    if any(targets[f].exists() for f in sources if sources[f].exists()):
        print(f"signer remove: {name} already has wallet key files in {key_dir}; not replaced.")
        return 1
    moved = [f for f in sources if sources[f].exists()]
    dotenv = installer().harvest_env(profile)
    if dotenv.get("EMPORIA_KEY_STORAGE", "").strip().lower() == "env":   # P206: where it keeps its keys
        from emporia.key_source import ENV_KEY_NAMES, SOURCE_VARS
        merge_env_file(profile / ".env", {
            k: v for fam in moved for k, v in ((ENV_KEY_NAMES[fam], agent_wallet.env_value(
                fam, sources[fam].read_bytes())), (SOURCE_VARS[fam], f"env:{ENV_KEY_NAMES[fam]}"))})
    else:
        key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for fam in moved:
            _write_secret_atomically(targets[fam], sources[fam].read_bytes())
            if os.getuid() == 0:   # P209: from the signer's admin container — the agent's own files are its user's
                owner = profile.stat()
                os.chown(targets[fam], owner.st_uid, owner.st_gid)
    if spent.exists():   # the 24-hour spend log comes back too, or a move out would reset the daily cap
        key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_secret_atomically(spent_here, spent.read_bytes())
        if os.getuid() == 0:
            owner = profile.stat()
            os.chown(spent_here, owner.st_uid, owner.st_gid)
    ident = signer.identity_path(name)
    if ident.exists():      # P293: the identity key comes back too, and the marker goes
        from emporia.identity import _keyfile_stem
        back = key_dir / f"{_keyfile_stem(name)}.priv"
        if back.exists():
            print(f"signer remove: {name} already has an identity key file at {back}; not replaced.")
            return 1
        key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_secret_atomically(back, ident.read_bytes())
        if os.getuid() == 0:
            owner = profile.stat()
            os.chown(back, owner.st_uid, owner.st_gid)
        (key_dir / f"{_keyfile_stem(name)}.signer.pub").unlink(missing_ok=True)
        moved.append("identity")
        enc = signer.encryption_path(name)
        if enc.exists():      # P293 (2b): and its encryption keys, the retired ones too
            for held_file, here in ((enc, key_dir / f"{_keyfile_stem(name)}.x25519.priv"),
                                    (enc.with_name("encryption.retired"),
                                     key_dir / f"{_keyfile_stem(name)}.x25519.retired")):
                if held_file.exists():
                    _write_secret_atomically(here, held_file.read_bytes())
                    if os.getuid() == 0:
                        owner = profile.stat()
                        os.chown(here, owner.st_uid, owner.st_gid)
            (key_dir / f"{_keyfile_stem(name)}.x25519.signer.pub").unlink(missing_ok=True)
            moved.append("encryption")
    unset_env_lines(profile / ".env", ["EMPORIA_SIGNER_URL", "EMPORIA_SIGNER_TOKEN"])
    merge_env_file(profile / ".env", {k: v for k, v in env.items()
                                      if k != "EMPORIA_X402_MAINNET" and k not in signer.SIGNING_POLICY})
    trash = signer.signer_dir() / "released" / f"{held.name}-{time.strftime('%Y%m%dT%H%M%S')}"
    trash.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.move(str(held), str(trash))
    where = "its .env" if dotenv.get("EMPORIA_KEY_STORAGE", "").strip().lower() == "env" else str(key_dir)
    print(f"{name}: its wallet ({', '.join(moved)}) is back in {where}, its limits in its .env. The signer's copy "
          f"is in {trash} until you remove it.")
    installer().print_restart_instructions([profile])
    return 0


# ─── P293 (signer phase 2): back ends, keys made inside, a backup ────────────

def _backends_lines() -> list[str]:
    """One line per back end: its type, what it does, whether it was ever tried live."""
    from emporia import signer, signer_backends as sb
    out, used = [], {}
    for agent, rs in signer.all_routes().items():
        for role, r in rs.items():
            if role in sb.ROLES:
                used.setdefault(r.get("backend"), []).append(f"{agent} {role}")
    for name, item in signer.backends().items():
        try:
            caps = ", ".join(sorted(signer.get_backend(name).capabilities))
        except sb.BackendError as e:
            caps = f"(not loaded: {e})"
        note = "" if sb.LIVE_TESTED.get(item["type"]) else f"  [{sb.NOT_LIVE_NOTE}]"
        holds = ", ".join(used.get(name, [])) or ("every role not routed elsewhere" if name == "local" else "nothing yet")
        out.append(f"  {name:12} {item['type']:8} {caps}{note}\n  {'':12} holds: {holds}")
    return out


def _signer_backend(args: argparse.Namespace) -> int:
    """P293: `emporia signer backend` — where the signer keeps each agent's keys
    (signer-phase-2.md § 3.1). Its person's, in their own terminal, as the
    signer's user; credentials go to the signer's folder only, asked hidden.

      emporia signer backend                              the back ends, and what each holds
      emporia signer backend NAME add aws-kms region=R [profile=P]
      emporia signer backend NAME add privy app_id=ID [policy_id=P] [owner_id=O]
      emporia signer backend NAME add cdp api_key_id=ID [policy_id=P]
      emporia signer backend NAME add metamask [chain_ids=84532] [verifying_contracts=0x…|any] [max_value=N]
                                                          its mm session (mm login, as the signer's user)
      emporia signer backend NAME add vault           a password manager (its token asked, hidden)
      emporia signer backend NAME use AGENT ROLE REF      a key already there (a KMS key id, a wallet id)
      emporia signer backend NAME make AGENT ROLE         a new key made there
      emporia signer backend NAME check                   ask it for each key's public half
      emporia signer backend NAME remove
    ROLE: identity, encryption, evm or svm. The signer's rules and limits apply
    whichever back end signs; a provider's own policy is a second layer."""
    import getpass

    from emporia import signer, signer_backends as sb
    name, rest = args.name, list(args.rest or [])
    if not name:
        print("The signer's back ends (signer-phase-2.md § 3.1):")
        for line in _backends_lines():
            print(line)
        print("Add: emporia signer backend NAME add vault|aws-kms|privy|cdp|metamask … · route a key: emporia signer backend NAME "
              "use|make AGENT ROLE")
        return 0
    if not _persons_terminal():
        print("signer backend: only a person sets where an agent's keys are kept, in their own terminal.")
        return 1
    verb = rest[0] if rest else "show"
    try:
        if verb == "show":
            item = signer.backends().get(name)
            if item is None:
                print(f"signer backend: no back end {name!r}")
                return 1
            d = signer.get_backend(name).describe()
            print(f"  {name}: {d['type']} — {', '.join(d['capabilities'])}" +
                  ("" if d["live_tested"] else f" [{sb.NOT_LIVE_NOTE}]"))
            for k, v in d["settings"].items():
                print(f"    {k} = {v}")
            if d["secrets"]:
                print(f"    secrets kept in the signer's folder: {', '.join(d['secrets'])}")
            return 0
        if verb == "add":
            if len(rest) < 2:
                print("signer backend: NAME add vault|aws-kms|privy|cdp|metamask key=value …")
                return 1
            type_ = rest[1]
            pairs = dict(p.split("=", 1) for p in rest[2:] if "=" in p)
            if bad := [p for p in rest[2:] if "=" not in p]:
                print(f"signer backend add: settings are key=value, not {' '.join(bad)}")
                return 1
            if type_ in sb.SECRETS and (named := set(pairs) & set(sb.SECRETS[type_])):
                print(f"signer backend add: {', '.join(sorted(named))} is a secret, never a value on the command "
                      "line — it is asked for, hidden")
                return 1
            secrets_ = {}
            for key in sb.SECRETS.get(type_, ()):
                value = getpass.getpass(f"  {key} (kept in the signer's folder only; Enter to skip): ").strip()
                if value:
                    secrets_[key] = value
            if type_ == "privy" and not secrets_.get("app_secret"):
                print("signer backend add: privy needs its app secret")
                return 1
            if type_ == "cdp" and not (secrets_.get("api_key_secret") and secrets_.get("wallet_secret")):
                print("signer backend add: cdp needs its API key secret and wallet secret")
                return 1
            signer.save_backend(name, type_, pairs, secrets_)
            print(f"  {name}: a {type_} back end, settings {', '.join(pairs) or 'none'}"
                  + (f", secrets {', '.join(secrets_)} in {signer.signer_dir() / 'backends'}" if secrets_ else "")
                  + ("" if sb.LIVE_TESTED.get(type_) else f".\n  Note: the {type_} adapter is {sb.NOT_LIVE_NOTE} "
                     "— try it on testnet first (docs/manual-testing.md sitting 7)."))
            return 0
        if verb == "remove":
            signer.remove_backend(name)
            print(f"  {name}: removed")
            return 0
        if verb == "check":
            found = [(a, r, v) for a, rs in signer.all_routes().items() for r, v in rs.items()
                     if r in sb.ROLES and v.get("backend") == name]
            b = signer.get_backend(name)
            for agent, role, v in found:
                print(f"  {agent} {role}: {b.public(agent, role, v.get('ref', ''))}")
            print(f"  {len(found)} key(s) answered" + ("" if b.live_tested else f" [{sb.NOT_LIVE_NOTE}]"))
            return 0
        if verb in ("use", "make"):
            if len(rest) < 3 or (verb == "use" and len(rest) < 4 and name != "local"):
                print(f"signer backend: NAME {verb} AGENT ROLE" + (" REF" if verb == "use" else ""))
                return 1
            agent, role = rest[1], rest[2]
            if not (signer.agent_dir(agent) / "policy.json").exists():
                print(f"signer backend: the signer holds no agent {agent!r} (emporia signer add {agent} first)")
                return 1
            if role not in sb.ROLES:
                print(f"signer backend: ROLE is one of {', '.join(sb.ROLES)}")
                return 1
            if role in ("identity", "encryption") and signer.holds(agent, role):
                print(f"signer backend: {agent} already has its {role} key; another key there would be another "
                      f"identity — " + ("emporia agent rotate-key makes the next one" if role == "identity"
                                        else "emporia agent rotate-encryption-key replaces it"))
                return 1
            before = signer.public(agent, role) if signer.holds(agent, role) else None
            b = signer.get_backend(name)
            ref = b.make(agent, role) if verb == "make" else (rest[3] if len(rest) > 3 else "")
            signer.set_route(agent, role, name, ref)
            now = signer.public(agent, role)
            print(f"  {agent} {role}: held by {name} ({b.type}) — {now}"
                  + ("" if b.live_tested else f"  [{sb.NOT_LIVE_NOTE}]"))
            if before and before != now:
                print(f"  Its {role} wallet was {before}: money there stays there, and the old key stays in the "
                      f"signer's folder. Register the new address for payouts (its agent's register_x402_wallet).")
            return 0
    except (ValueError, sb.BackendError) as e:
        print(f"signer backend: {e}")
        return 1
    print("signer backend: NAME add|use|make|check|show|remove")
    return 1


def _signer_add_make(args: argparse.Namespace, url: str) -> int:
    """P293 (2c): `emporia signer add NAME --make` — a new agent's identity,
    encryption and wallet keys made inside the signer (or the back ends named
    with --backend ROLE=NAME), so they never exist in its profile. The profile
    gets only their public halves (the `.signer.pub` markers) and the signer's
    address; the agent then registers, signing through the signer."""
    from emporia import signer, signer_backends as sb
    from emporia.identity import _keyfile_stem
    name = args.name
    profile = profile_dir(name)
    env = {**installer().harvest_env(profile), **_mcp_env(profile)}
    if env.get("EMPORIA_SIGNER_URL"):
        print(f"signer add --make: {name} is already behind the signer at {env['EMPORIA_SIGNER_URL']}")
        return 1
    for var in ("EMPORIA_IDENTITY_KEY_SOURCE", "EMPORIA_ENCRYPTION_KEY_SOURCE", "EMPORIA_X402_EVM_KEY_SOURCE",
                "EMPORIA_X402_SOLANA_KEY_SOURCE", "EMPORIA_IDENTITY_PRIVATE_KEY", "EMPORIA_ENCRYPTION_PRIVATE_KEY",
                "EMPORIA_X402_EVM_PRIVATE_KEY", "EMPORIA_X402_SOLANA_PRIVATE_KEY"):
        if env.get(var, "").strip():
            print(f"signer add --make: {name} already has a key ({var} is set) — keys made in the signer are for an "
                  f"agent that has none yet. To move the keys it has: emporia signer add {name} --identity")
            return 1
    key_dir = Path(env.get("EMPORIA_KEYS_DIR") or profile / "home" / ".hermes" / "keys")
    stem = _keyfile_stem(name)
    have = [f.name for f in (key_dir.glob(f"{stem}.*") if key_dir.is_dir() else [])
            if f.is_file() and not f.name.endswith(".signer.pub")
            and any(f.name.endswith(s) for s in (".priv", ".x25519.priv", ".x25519.retired", ".x402.secp256k1",
                                                  ".x402.ed25519", ".x402.phrase"))]
    if have:
        print(f"signer add --make: {name} already has key files ({', '.join(sorted(have))}) — keys made in the "
              f"signer are for an agent that has none yet. To move them: emporia signer add {name} --identity")
        return 1
    if (signer.agent_dir(name) / "policy.json").exists():
        print(f"signer add --make: the signer already holds an agent {name!r}")
        return 1
    where = {}
    for item in getattr(args, "backend", None) or []:
        role, _, backend = item.partition("=")
        if role not in sb.ROLES or not backend:
            print(f"signer add --make: --backend ROLE=NAME, ROLE one of {', '.join(sb.ROLES)}")
            return 1
        where[role] = backend
    try:
        made = signer.make_agent_keys(name, where)
    except (ValueError, sb.BackendError) as e:
        print(f"signer add --make: {e}")
        return 1
    rules = {**env, **{k: v for k, v in _mcp_env(profile).items() if k in signer.SIGNING_POLICY}}
    signer.save_policy(name, {k: rules.get(k, "") for k in signer.POLICY_NAMES if k != "EMPORIA_X402_MAINNET"})
    token = signer.new_agent_token(name)
    if added := signer.add_settings(env):
        print(f"  The signer takes {name}'s network settings: {', '.join(added)} (in {signer.settings_path()})")
    signer.own(signer.agent_dir(name))
    key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for marker, value in ((key_dir / f"{stem}.signer.pub", made["identity"]),
                          (key_dir / f"{stem}.x25519.signer.pub", made["encryption"])):
        marker.write_text(value + "\n")
        if os.getuid() == 0:
            owner = profile.stat()
            os.chown(marker, owner.st_uid, owner.st_gid)
    merge_env_file(profile / ".env", {"EMPORIA_SIGNER_URL": url, "EMPORIA_SIGNER_TOKEN": token})
    where_said = {r: where.get(r, "local") for r in made}
    print(f"{name}: its keys were made in the signer and never existed in its profile:")
    for role, pub in made.items():
        b = signer.get_backend(where_said[role])
        print(f"    {role:10} {pub}   ({where_said[role]}"
              + ("" if b.live_tested else f"; {sb.NOT_LIVE_NOTE}") + ")")
    print(f"  The profile has only the public halves ({stem}.signer.pub, {stem}.x25519.signer.pub) and asks {url}.\n"
          f"  Back them up from the signer's side: emporia signer backup {name} --out PATH")
    r = _as_agent(profile, "from emporia import mcp_server as m\n"
                  "r = m.register_agent(agent_id=m.AGENT_ID)\nprint('ok' if r.get('success') else r.get('error'))")
    last = (r.stdout or "").strip().splitlines()[-1:] if r.returncode == 0 else []
    if last == ["ok"]:
        print("  Published: it registered on its relay, signed by the signer (its identity and encryption keys).")
    else:
        print(f"  Not published yet ({((r.stdout or r.stderr) or '').strip()[-160:]}); it registers when it next "
              "starts, with the signer running.")
    installer().print_restart_instructions([profile])
    return 0


def _signer_add_sources(args: argparse.Namespace, url: str) -> int:
    """P293: `emporia signer add NAME --key-source ROLE=cmd:…` — an agent whose
    keys are kept in a password manager goes behind the signer, which reads them
    itself (`cmd:op read op://…`, `cmd:bw get password ITEM`) at its start. The
    vault token becomes the signer's; the agent's profile keeps no reference to
    the vault, only the public halves. Each key read must be the one the agent
    has now, or nothing changes. Its person's, in their own terminal."""
    import getpass

    from emporia import signer, signer_backends as sb
    from emporia.identity import _keyfile_stem
    from emporia.key_source import SOURCE_VARS, unset_env_lines
    name = args.name
    profile = profile_dir(name)
    env = {**installer().harvest_env(profile), **_mcp_env(profile)}
    if env.get("EMPORIA_SIGNER_URL"):
        print(f"signer add --key-source: {name} is already behind the signer at {env['EMPORIA_SIGNER_URL']}")
        return 1
    if (signer.agent_dir(name) / "policy.json").exists():
        print(f"signer add --key-source: the signer already holds an agent {name!r}")
        return 1
    refs: dict[str, str] = {}
    for item in args.key_source:
        role, _, ref = item.partition("=")
        if role not in sb.ROLES or not ref.strip().startswith("cmd:"):
            print(f"signer add --key-source: \"ROLE=cmd:…\", quoted (ROLE one of {', '.join(sb.ROLES)}), e.g. "
                  "--key-source \"identity=cmd:op read op://Agents/alpha/identity\"")
            return 1
        refs[role] = ref.strip()
    key_dir = Path(env.get("EMPORIA_KEYS_DIR") or profile / "home" / ".hermes" / "keys")
    stem = _keyfile_stem(name)
    files = {"identity": f"{stem}.priv", "encryption": f"{stem}.x25519.priv", "evm": f"{stem}.x402.secp256k1",
             "svm": f"{stem}.x402.ed25519"}
    has = {r for r in sb.ROLES if env.get(SOURCE_VARS[r], "").strip() or (key_dir / files[r]).is_file()}
    if missing := sorted(has - set(refs)):
        print(f"signer add --key-source: {name} also has {', '.join(missing)} keys; name a source for each "
              "(every key moves together), or move key files with: emporia signer add NAME --identity")
        return 1
    if ("identity" in refs) != ("encryption" in refs):
        print("signer add --key-source: the identity and encryption keys move together")
        return 1
    if "vault" not in signer.backends():
        print("  The signer reads them with the vault's own command-line tool. Its token is kept in the signer's "
              "folder\n  (Enter to skip one: the signer's own environment then has to provide it).")
        tokens = {k: getpass.getpass(f"  {k} (hidden; Enter to skip): ").strip()
                  for k in sb.SECRETS["vault"]}
        signer.save_backend("vault", "vault", {}, {k: v for k, v in tokens.items() if v})
    b = signer.get_backend("vault")
    publics = {}
    try:
        for role, ref in refs.items():
            publics[role] = b.public(name, role, ref)
    except sb.BackendError as e:
        print(f"signer add --key-source: {e}")
        return 1
    for role in ("identity", "encryption"):
        if role in publics and (want := _expected_public(profile, name, role)) and want != publics[role]:
            print(f"signer add --key-source: the {role} key in {refs[role]} is not {name}'s ({want[:12]}…); "
                  "it would be another agent. Nothing changed.")
            return 1
    for role, ref in refs.items():
        signer.set_route(name, role, "vault", ref)
    rules = {**env, **{k: v for k, v in _mcp_env(profile).items() if k in signer.SIGNING_POLICY}}
    signer.save_policy(name, {k: rules.get(k, "") for k in signer.POLICY_NAMES if k != "EMPORIA_X402_MAINNET"})
    token = signer.new_agent_token(name)
    signer.add_settings(env)
    signer.own(signer.agent_dir(name))
    if "identity" in publics:
        key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for marker, value in ((key_dir / f"{stem}.signer.pub", publics["identity"]),
                              (key_dir / f"{stem}.x25519.signer.pub", publics["encryption"])):
            marker.write_text(value + "\n")
    unset_env_lines(profile / ".env", [SOURCE_VARS[r] for r in refs] + ["EMPORIA_X402_MAINNET"])
    left = [v for v in SOURCE_VARS.values() if installer().harvest_env(profile).get(v, "").startswith("cmd:")]
    vault_vars = [v for v in ("OP_SERVICE_ACCOUNT_TOKEN", "BW_SESSION", "BW_CLIENTID", "BW_CLIENTSECRET")
                  if installer().harvest_env(profile).get(v)]
    if vault_vars and not left:
        unset_env_lines(profile / ".env", vault_vars)
    merge_env_file(profile / ".env", {"EMPORIA_SIGNER_URL": url, "EMPORIA_SIGNER_TOKEN": token})
    print(f"{name}: its keys ({', '.join(refs)}) are read by the signer from the password manager; its profile "
          "names no vault any more.")
    if vault_vars and not left:
        print(f"  The vault's token is the signer's now: removed from {name}'s profile ({', '.join(vault_vars)}).")
    print("  The signer reads them when it starts and holds them in memory while it runs: safer than files, "
          "less than KMS.\n  Restart the signer (emporia signer stop; emporia signer start) to read them now.")
    installer().print_restart_instructions([profile])
    return 0


def _signer_backup(args: argparse.Namespace) -> int:
    """P293 (2c): `emporia signer backup NAME --out PATH` — the agent's keys the
    signer holds, sealed under a passphrase, for its person to keep off this
    machine. Run from the signer's side, in the person's own terminal."""
    import getpass

    from emporia import signer
    from emporia.identity import _write_secret_atomically
    if not _persons_terminal():
        print("signer backup: only a person backs up an agent's keys, in their own terminal.")
        return 1
    if not args.name or not (signer.agent_dir(args.name) / "policy.json").exists():
        print(f"signer backup: the signer holds no agent {args.name!r}")
        return 1
    out = Path(args.out).expanduser() if args.out else None
    if out is None or out.exists() or not out.parent.is_dir():
        print("signer backup: --out PATH, a file that does not exist yet in a folder that does")
        return 1
    phrase = getpass.getpass("  A passphrase for the backup (12 characters or more; nothing is shown): ")
    if getpass.getpass("  Again: ") != phrase:
        print("signer backup: the two did not match; nothing written")
        return 1
    try:
        data, notes = signer.backup(args.name, phrase)
    except ValueError as e:
        print(f"signer backup: {e}")
        return 1
    _write_secret_atomically(out, data)
    print(f"  {args.name}: its keys in the signer, sealed under your passphrase, in {out} (owner-only).\n"
          "  Keep it off this machine (and the passphrase apart from it); restore with "
          f"emporia signer restore {args.name} --from PATH.")
    for note in notes:
        print(f"  {note}")
    return 0


def _signer_restore(args: argparse.Namespace) -> int:
    import getpass

    from emporia import signer
    if not _persons_terminal():
        print("signer restore: only a person restores an agent's keys, in their own terminal.")
        return 1
    src = Path(getattr(args, "from_", "") or "").expanduser()
    if not args.name or not src.is_file():
        print("signer restore: NAME --from PATH (a backup from emporia signer backup)")
        return 1
    try:
        roles = signer.restore(args.name, src.read_bytes(), getpass.getpass("  The backup's passphrase: "))
    except ValueError as e:
        print(f"signer restore: {e}")
        return 1
    print(f"  {args.name}: {', '.join(roles)} back in the signer. If the agent is new to this signer, its policy "
          f"and token follow with: emporia signer add {args.name} (it is already behind a signer: rerun is refused)")
    return 0


def _signer_policy(args: argparse.Namespace) -> int:
    import re

    from emporia import relay_config, signer
    from emporia.networks import is_testnet, mainnet_unlocked, resolve_list
    name = args.name
    if not name or not (signer.agent_dir(name) / "policy.json").exists():
        print(f"signer policy: the signer holds no agent {name!r}")
        return 1
    env = dict(signer.policy(name).get("env", {}))
    rest = args.rest or []
    if not rest:
        for k in signer.POLICY_NAMES:
            print(f"  {k:34} {env.get(k, '—')}")
        print(f"Change: emporia signer policy {name} set NAME VALUE · emporia signer policy {name} mainnet [--lock]")
        return 0
    if rest[0] == "mainnet":
        if args.lock:
            env["EMPORIA_X402_MAINNET"] = "0"
            env["EMPORIA_X402_PAY_NETWORKS"] = ",".join(
                n for n in resolve_list(env.get("EMPORIA_X402_PAY_NETWORKS", "")) if is_testnet(n))
            signer.save_policy(name, env)
            print(f"{name}: mainnet locked in the signer — testnets only.")
            return 0
        if not _persons_terminal():
            print("signer policy mainnet: only a person turns on real money, in their own terminal.")
            return 1
        # P206: real money needs the signer apart from the agent. As the same
        # user, the agent's own processes can read the signer's folder — and so
        # approve its own payments or raise its limits.
        profile = profile_dir(name)
        # P209: judged where the agents run. In the signer's admin container (root,
        # Hermes's data mounted) this machine is not theirs: Hermes's own
        # `emporia signer status` and doctor judge it there.
        from emporia.relay_host import in_container
        ways_round = _reachable_anyway(False) if profile.exists() and not in_container() else []
        if in_container():
            print("  (Run `emporia signer status` from Hermes too: it checks that Hermes's user cannot reach the "
                  "signer.)")
        if ways_round and os.getenv("EMPORIA_ALLOW_HOT_AGENT_WALLET", "").strip() != "1":
            print(f"signer policy mainnet: the agents' user could reach the signer anyway — {'; '.join(ways_round)}. "
                  f"Close that first; testnet works as it is.")
            return 1
        if (profile.exists() and profile.stat().st_uid == signer.signer_dir().stat().st_uid
                and os.getenv("EMPORIA_ALLOW_HOT_AGENT_WALLET", "").strip() != "1"):
            print(f"signer policy mainnet: the signer's folder belongs to the same user as {name}'s profile, so "
                  f"{name}'s own processes could approve its payments and change its limits. Run the signer as its "
                  f"own user or container (docs/agent-security-setup.md § The signer), add {name} there, then "
                  f"unlock. Testnet works as it is.")
            return 1
        cap, day = env.get("EMPORIA_X402_MAX_PAYMENT_CENTS", "1000"), env.get("EMPORIA_X402_MAX_DAILY_CENTS", "5000")
        print(f"  Mainnet means real money. Caps: ${int(cap) / 100:.2f} a payment, ${int(day) / 100:.2f} a day; "
              f"payments above ${int(env.get('EMPORIA_PAYMENT_APPROVAL_CENTS', '0')) / 100:.2f} wait for "
              f"`emporia signer approve`. Fund its wallet with only what you are ready to lose.")
        nets = prompt("Mainnet networks it may pay on (e.g. eip155:8453,solana:mainnet)", "")
        if prompt(f"Type the agent's name ({name}) to let it pay real money").strip() != name:
            print("Not unlocked.")
            return 1
        env["EMPORIA_X402_MAINNET"] = "1"
        env["EMPORIA_X402_PAY_NETWORKS"] = ",".join(
            dict.fromkeys(resolve_list(env.get("EMPORIA_X402_PAY_NETWORKS", "")) + resolve_list(nets)))
        signer.save_policy(name, env)
        print(f"{name}: mainnet unlocked in the signer — {env['EMPORIA_X402_PAY_NETWORKS']}.")
        return 0
    if rest[0] != "set" or len(rest) != 3 or rest[1] not in signer.POLICY_NAMES or rest[1] == "EMPORIA_X402_MAINNET":
        print(f"signer policy: set NAME VALUE, NAME one of {', '.join(n for n in signer.POLICY_NAMES if n != 'EMPORIA_X402_MAINNET')}"
              f"; mainnet with `emporia signer policy {name} mainnet`")
        return 1
    _, key, value = rest
    if key in signer.SIGNING_POLICY:
        from emporia import signing
        try:
            if key == "EMPORIA_SIGN_LIMITS":
                signing.limits({key: value})
            elif key in ("EMPORIA_RELAY_URL", "EMPORIA_BACKUP_RELAYS", "EMPORIA_VISIT_RELAYS"):
                value = relay_config.check(relay_config.Setting(key, "", "", "urls"), value) if value else ""
            elif not all(re.fullmatch(r"0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44}", a.strip())
                         for a in value.split(",") if a.strip()):
                raise ValueError("EVM (0x…) or Solana addresses, comma-separated")
        except ValueError as e:
            print(f"signer policy: {key}: {e}")
            return 1
        env[key] = value
        signer.save_policy(name, env)
        print(f"  {name}: {key}={value} (applies to its next signature)")
        return 0
    if key == "EMPORIA_X402_PAY_NETWORKS":
        from emporia.networks import NETWORKS
        try:
            nets = resolve_list(value)
        except (KeyError, ValueError) as e:
            print(f"signer policy: {key}: {e}")
            return 1
        unknown = [n for n in nets if n not in NETWORKS]
        if unknown or not nets:
            print(f"signer policy: {key}: not networks Emporia knows: {', '.join(unknown) or value!r}")
            return 1
        locked = [n for n in nets if not is_testnet(n)] and not mainnet_unlocked(env)
        if locked:
            print(f"signer policy: mainnet networks need `emporia signer policy {name} mainnet` first")
            return 1
        value = ",".join(nets)
    else:
        try:
            value = relay_config.check(relay_config.Setting(key, "", "", "int"), value)
        except ValueError as e:
            print(f"signer policy: {key}: {e}")
            return 1
    env[key] = value
    signer.save_policy(name, env)
    print(f"  {name}: {key}={value} (applies to its next payment)")
    return 0


def _signer_add(args: argparse.Namespace, url: str) -> int:
    """Move an agent's wallet keys and limits into the signer and point it there."""
    import json as _json

    from emporia import signer
    from emporia.agent_wallet import wallet_path
    from emporia.identity import _write_secret_atomically
    from emporia.key_source import unset_env_lines
    name = args.name
    profile = profile_dir(name)
    if not name or not (profile / "config.yaml").exists():
        print(f"signer add: no agent profile {name!r}")
        return 1
    if not _persons_terminal():
        print("signer add: only a person moves an agent's keys, in their own terminal.")
        return 1
    if getattr(args, "make", False):
        return _signer_add_make(args, url)
    if getattr(args, "key_source", None):
        return _signer_add_sources(args, url)
    dotenv = installer().harvest_env(profile)
    if dotenv.get("EMPORIA_SIGNER_URL"):
        if getattr(args, "identity", False):
            return _signer_add_identity(profile, name)
        print(f"signer add: {name} is already behind the signer at {dotenv['EMPORIA_SIGNER_URL']}"
              + ("" if (signer.identity_path(name)).exists() else
                 f" — its identity key can follow: emporia signer add {name} --identity"))
        return 1
    if any(str(dotenv.get(v, "")).startswith("cmd:") for v in ("EMPORIA_X402_EVM_KEY_SOURCE",
                                                               "EMPORIA_X402_SOLANA_KEY_SOURCE")):
        print(f"signer add: {name}'s wallet is linked to a password manager; the signer reads only its own "
              f"key files for now. Not moved.")
        return 1
    if _phrase_state(profile, name) == "file":
        print(f"signer add: {name}'s recovery phrase is still stored here. Write it down and remove it first: "
              f"emporia wallet backup {name}")
        return 1
    # The keys, as the agent's own process reads them (files or its .env).
    r = _as_agent(profile, "import json\nfrom emporia import agent_wallet as w\nout={}\n"
                  f"aid={name!r}\n"
                  "for f in w._families(aid):\n"
                  "    out[f] = w.load_key(aid)[2:] if f == 'evm' else w._load_solana_seed(aid).hex()\n"
                  "print(json.dumps(out))")
    try:
        keys = _json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(f"signer add: could not read {name}'s wallet keys — {r.stderr.strip()[-300:]}")
        return 1
    if not keys:
        print(f"signer add: {name} has no wallet to move")
        return 1
    from emporia import agent_wallet
    scope = agent_wallet.Scope(env={}, key_dir=signer.agent_dir(name) / "keys")
    with agent_wallet.scope(scope):
        targets = {"evm": wallet_path(name), "svm": agent_wallet.solana_wallet_path(name)}
        if any(targets[f].exists() for f in keys):
            print(f"signer add: the signer already has a wallet for {name}; not replaced.")
            return 1
        targets["evm"].parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for fam, hexkey in keys.items():
            _write_secret_atomically(targets[fam], bytes.fromhex(hexkey))
    rules = {**dotenv, **{k: v for k, v in _mcp_env(profile).items() if k in signer.SIGNING_POLICY}}
    signer.save_policy(name, {k: rules.get(k, "") for k in signer.POLICY_NAMES if k != "EMPORIA_X402_MAINNET"})
    token = signer.new_agent_token(name)
    if added := signer.add_settings(dotenv):   # P206: the RPCs it paid through
        print(f"  The signer takes {name}'s network settings: {', '.join(added)} (in {signer.settings_path()})")
    if os.getuid() == 0:     # run as root in the signer's container: its files are the signer's user's
        owner = signer.signer_dir().stat()
        for path in [signer.agent_dir(name), *signer.agent_dir(name).rglob("*")]:
            os.chown(path, owner.st_uid, owner.st_gid)
    # P207: the signer's copy checked, then the old copies destroyed — a trash
    # folder in the profiles is still within the agents' reach. The 24-hour spend
    # log goes with the keys, or the move would reset the daily cap.
    key_dir = Path(_mcp_env(profile).get("EMPORIA_KEYS_DIR") or profile / "home" / ".hermes" / "keys")
    with agent_wallet.scope(scope):
        if any(targets[f].read_bytes() != bytes.fromhex(h) for f, h in keys.items()):
            print("signer add: the signer's copy does not read back as written; nothing removed from the profile.")
            return 1
        ledger = agent_wallet._ledger_path(name)
    from emporia.identity import _keyfile_stem
    old = [f for f in key_dir.glob(f"{_keyfile_stem(name)}.x402.*") if f.is_file()]
    for f in old:
        if f.name.endswith(".spend.json"):
            _write_secret_atomically(ledger, f.read_bytes())
    signer.own(signer.agent_dir(name))
    for f in old:
        with open(f, "r+b") as out:     # overwrite, then unlink
            out.write(b"\0" * f.stat().st_size)
            out.flush()
            os.fsync(out.fileno())
        f.unlink()
    unset_env_lines(profile / ".env", ["EMPORIA_X402_EVM_PRIVATE_KEY", "EMPORIA_X402_SOLANA_PRIVATE_KEY",
                                       "EMPORIA_X402_EVM_KEY_SOURCE", "EMPORIA_X402_SOLANA_KEY_SOURCE",
                                       "EMPORIA_X402_MAINNET"])
    merge_env_file(profile / ".env", {"EMPORIA_SIGNER_URL": url, "EMPORIA_SIGNER_TOKEN": token})
    print(f"{name}: its wallet ({', '.join(keys)}) is now held by the signer at {signer.signer_dir()}; its "
          f"limits moved with it (emporia signer policy {name}). The agent asks {url} with its own token.")
    if old:
        print(f"  Its old key files in the profile were destroyed ({len(old)}); the signer's copy is the only one.")
    print(f"  For real money the signer runs as its own user (or container): check with emporia signer status. "
          f"Then, as the signer's user: emporia signer policy {name} mainnet")
    if getattr(args, "identity", False):
        return _signer_add_identity(profile, name)
    print(f"  Its identity key can follow (signer phase 2): emporia signer add {name} --identity")
    installer().print_restart_instructions([profile])
    return 0


def _signer_add_identity(profile: Path, name: str) -> int:
    """P293 (signer phase 2a): the agent's identity key moves into the signer;
    the profile keeps only its public half (`<agent>.signer.pub`). From then on
    every signature is the signer's, under its rules (docs/proposals/signer-phase-2.md)."""
    from emporia import signer
    from emporia.identity import _keyfile_stem, _write_secret_atomically
    env = {**installer().harvest_env(profile), **_mcp_env(profile)}
    for linked, what in ((env.get("EMPORIA_IDENTITY_KEY_SOURCE", ""), "identity"),
                         (env.get("EMPORIA_ENCRYPTION_KEY_SOURCE", ""), "encryption")):
        if linked.strip():
            print(f"signer add: {name}'s {what} key is linked ({linked.split(':')[0]}:…); the signer takes only "
                  f"key files for now. Not moved.")
            return 1
    if signer.identity_path(name).exists():
        print(f"signer add: the signer already holds {name}'s identity key")
        return 1
    key_dir = Path(env.get("EMPORIA_KEYS_DIR") or profile / "home" / ".hermes" / "keys")
    src = key_dir / f"{_keyfile_stem(name)}.priv"
    if not src.is_file() or src.stat().st_size != 32:
        print(f"signer add: no identity key file for {name} at {src}; nothing moved.")
        return 1
    enc_src = key_dir / f"{_keyfile_stem(name)}.x25519.priv"
    enc_retired = key_dir / f"{_keyfile_stem(name)}.x25519.retired"
    if not enc_src.is_file() or enc_src.stat().st_size != 32:
        print(f"signer add: no encryption key file for {name} at {enc_src}; nothing moved.")
        return 1
    raw = src.read_bytes()
    enc_raw = enc_src.read_bytes()
    from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
    pub = ed25519.Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw().hex()
    enc_pub = x25519.X25519PrivateKey.from_private_bytes(enc_raw).public_key().public_bytes_raw().hex()
    dest, enc_dest = signer.identity_path(name), signer.encryption_path(name)
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_secret_atomically(dest, raw)
    _write_secret_atomically(enc_dest, enc_raw)
    if enc_retired.is_file():    # P271: the keys it replaced, for messages sealed before
        _write_secret_atomically(enc_dest.with_name("encryption.retired"), enc_retired.read_bytes())
    if dest.read_bytes() != raw or enc_dest.read_bytes() != enc_raw:
        print("signer add: the signer's copy does not read back as written; nothing removed from the profile.")
        return 1
    signer.own(signer.agent_dir(name))
    for marker, value in ((key_dir / f"{_keyfile_stem(name)}.signer.pub", pub),
                          (key_dir / f"{_keyfile_stem(name)}.x25519.signer.pub", enc_pub)):
        marker.write_text(value + "\n")
        if os.getuid() == 0:
            owner = profile.stat()
            os.chown(marker, owner.st_uid, owner.st_gid)
    for f in (src, enc_src, enc_retired):   # overwrite, then unlink: the signer's copies are the only ones
        if f.is_file():
            with open(f, "r+b") as out:
                out.write(b"\0" * f.stat().st_size)
                out.flush()
                os.fsync(out.fileno())
            f.unlink()
    print(f"{name}: its identity and encryption keys are now held by the signer; the profile keeps only their "
          f"public halves ({pub[:12]}…, {enc_pub[:12]}…). Every signature is the signer's, within its rules, and "
          f"every sealed message is opened there — emporia signer policy {name} shows the rules, "
          f"emporia signer log {name} what it signed and how many it opened.")
    installer().print_restart_instructions([profile])
    return 0


def cmd_agent_secure(name: str) -> int:
    """P201: `emporia agent secure NAME|all` — every secret out of the profile's
    config.yaml into its .env (Hermes never reads config.yaml's `env:`; an MCP
    server's entry becomes `${NAME}`, filled from the .env), and both files
    owner-only. Names are printed, never values. Restart the gateway after."""
    root = profiles_root()
    targets = sorted(p.parent for p in root.glob("*/config.yaml")) if name == "all" else [root / name]
    if not targets or not (targets[0] / "config.yaml").exists():
        print(f"agent secure: no profile {name!r} under {root}")
        return 1
    any_moved = False
    for profile in targets:
        moved = installer().secure_profile(profile)
        any_moved = any_moved or bool(moved)
        print(f"  {profile.name}: " + (f"moved to .env — {', '.join(moved)}" if moved else "nothing in config.yaml")
              + "; config.yaml and .env owner-only")
    if any_moved:
        print("Restart the gateway so each profile's MCP servers are started again.")
    return 0


def cmd_agent_refresh(name: str) -> int:
    """P224: `emporia agent refresh NAME|all` — the Emporia plugin copied fresh into
    each profile that has one (P295: the one plugin, carrying its skills and the
    dashboard tab), the old copies of its skills removed. No questions: what
    doctor's "older copy" warnings ask for, without the whole setup wizard.
    Installed from the catalog, the plugin is the catalog's: `hermes plugins update emporia`."""
    # Only profiles that are Emporia agents (an `emporia` MCP server in their config):
    # never one that merely shows the dashboard tab, like a Hermes root left alone.
    root = profiles_root().parent
    targets = ([p for p, _ in _agent_profiles(profiles_root())] if name == "all" else [profile_dir(name)])
    if not targets or not (targets[0] / "config.yaml").exists():
        print(f"agent refresh: no profile {name!r} under {root}" if name != "all"
              else f"agent refresh: no profile under {root} has the Emporia plugin")
        return 1
    inst = installer()
    # Catalog rule 11: each agent's MCP entry names every setting Hermes hands its server.
    for profile in targets:
        if added := inst.refresh_mcp_references(profile):
            print(f"  {profile.name}: {len(added)} setting(s) now handed to its MCP server by Hermes "
                  "(restart the gateway, or /reload-mcp)")
    if inst.from_catalog():
        print("agent refresh: the plugin and its skills came with the catalog — update them with "
              "`hermes plugins update emporia`, then `hermes emporia update`")
        return 0
    for profile in targets:
        print(f"  {profile.name}:")
        inst.install_platform_plugin(profile)
        inst._install_skill_link(profile)
    print("Restart the gateway so each agent loads the new plugin and skills: hermes gateway restart")
    return 0


def cmd_agent_inbox_clear(name: str) -> int:
    """P235: `emporia agent inbox-clear NAME|all` — every unread event in an
    agent's relay inbox marked read, once. For the backlog from before an
    agent's bridge marked what it delivered (P233): old turn notices of games
    long over. Run by a person; the dashboard marks nothing read (P234)."""
    names = [p.name for p, _ in _agent_profiles(profiles_root())] if name == "all" else [name]
    if not names:
        print(f"agent inbox-clear: no agent profile under {profiles_root()}")
        return 1
    failed = False
    for n in names:
        profile, agent_id, _ = _agent(n)
        key_dir = _key_file(profile, agent_id, "identity").parent
        status, body = _signed_get(agent_id, key_dir, f"agents/{agent_id}/inbox/mark-all-read", "POST")
        if status == 200:
            print(f"  {n}: {body.get('marked', 0)} marked read")
        else:
            failed = True
            print(f"  {n}: not cleared ({status or body}) — is the relay running current code? "
                  "emporia relay restart")
    return 1 if failed else 0


def cmd_agent_rotate_key(name: str, yes: bool = False, http: Any = None) -> int:
    """P245: `emporia agent rotate-key NAME` — a new identity key for an agent,
    the change signed by its old key and added to the key log its relay serves,
    so everyone who pinned the old key follows it and what it signed still
    checks. The agent keeps its name and identity. Only a person runs it.

    The new key is written to disk before the relay hears of it. A key kept in
    a file is replaced and the old file kept beside it (`.rotated-<time>`, never
    deleted); a key kept in a password manager, an environment variable or the
    profile's .env is not touched: the new one is left in a 0600 file to store
    there. Then the agent registers again, which signs its encryption-key
    binding and relay list with the new key. `http` is for tests."""
    import httpx

    from emporia import key_rotation
    found = _agent_to_change("rotate-key", name)
    if not found:
        return 1
    profile, agent_id, _env, dotenv, source, key_dir = found
    print(f"This gives {agent_id} a new identity key on {relay_url()}. The old key signs the change, so peers that "
          "pinned it follow, and what it signed still checks.\n"
          "If the old key LEAKED or is lost, this does not recover it (whoever holds it can sign a change too): "
          "make a recovery key first, with `emporia agent recovery-key`.")
    if not (yes or ask(f"Rotate {agent_id}'s key?", False)):
        return 1
    client = http or httpx.Client(base_url=relay_url(), timeout=15)
    try:
        out = key_rotation.rotate_identity_key(agent_id, key_dir, client, source=source, source_env=dotenv)
    except key_rotation.RotationError as e:
        print(f"agent rotate-key: {e}")
        return 1
    finally:
        if http is None:
            client.close()
    _after_key_change(profile, agent_id, out, source, dotenv)
    return 0


def _after_key_change(profile: Path, agent_id: str, out: dict[str, Any], source: str,
                      dotenv: dict[str, str], *, recovered: bool = False) -> None:
    """What the person is told, and the agent does, once the relay has its new
    identity key (a rotation or a recovery): where the key is, then the agent
    registers again, which signs its encryption-key binding and relay list with it."""
    import shlex

    from emporia.addresses import fingerprint
    from emporia.identity import IDENTITY_KEY_SOURCE_VAR
    print(f"  {agent_id}: new key {fingerprint(out['new_public_key'])} (was {fingerprint(out['old_public_key'])}), "
          "on the relay.")
    republish_env: dict[str, str] = {}
    if out.get("in_signer"):
        print("  The new key was made in its signer and stays there; the profile has only its public half.")
    elif out["installed"]:
        print(f"  The new key is {out['new_key_file']}.")
        if out["kept_old_key_file"] and recovered:
            print(f"  The old key is kept as {out['kept_old_key_file']}. That key is compromised: it is kept only "
                  "as evidence, the relay refuses what it signs from the time you gave onward, and nothing needs it. "
                  "You may delete the file.")
        elif out["kept_old_key_file"]:
            print(f"  The old key is kept as {out['kept_old_key_file']}. It is not compromised, just no longer used "
                  "(the relay still accepts what it signed before). You may delete the file once you are satisfied "
                  "the agent works with the new one.")
    else:
        print(f"  Its key is kept in {source!r}, which I did not touch. The new private key is in "
              f"{out['new_key_file']} (owner-only): store it in that source, then remove the file. Until you do, "
              "the agent cannot sign in.")
        republish_env = {IDENTITY_KEY_SOURCE_VAR: f"cmd:cat {shlex.quote(str(out['new_key_file']))}"}
    # Registering again publishes its encryption key and relay list signed with the new key.
    r = _as_agent(profile, "from emporia import mcp_server as m\n"
                  "r = m.register_agent(agent_id=m.AGENT_ID)\nprint('ok' if r.get('success') else r.get('error'))",
                  **republish_env)
    if r.returncode != 0 or r.stdout.strip().splitlines()[-1:] != ["ok"]:
        print(f"  It could not sign its encryption key and relay list with the new key yet "
              f"({(r.stdout or r.stderr).strip()[-200:]}); they are signed again when the agent next starts.")
    else:
        print("  Its encryption key and relay list are signed again with the new key.")
    installer().print_restart_instructions([profile])


def _show_playbook(name: str, pb) -> None:
    from emporia import playbook as P
    games = pb.games if isinstance(pb.games, str) else ", ".join(g.split(":")[1] for g in pb.games)
    stake = "its spending limits" if pb.max_stake_cents is None else f"${pb.max_stake_cents / 100:.2f}"
    print(f"  Playbook    {pb.preset} — {P.about(pb)}")
    print(f"  Games       {games}; stakes up to {stake}")
    print(f"  Answers     {pb.answers}   Sells {'yes' if pb.sells else 'no'}   Buys {'yes' if pb.buys else 'no'}"
          f"   Runs on {pb.runs}   Pledges {'yes' if pb.pledges else 'no'}")


def _setup_playbook(profile: Path, name: str, chosen: str, existing: bool) -> None:
    """P297: setup's question. A new agent is asked (custom first: its own instructions
    decide); an existing one keeps its playbook unless --playbook says otherwise."""
    from emporia import playbook as P
    current = installer().harvest_env(profile).get("EMPORIA_PLAYBOOK", "")
    if not chosen and (existing and current or not _TTY):
        return
    if not chosen:
        heading("Playbook", "What it may do on its own: games, stakes, selling, buying. Never its style.")
        chosen = choose("What should it do?", [(k, P.ABOUT[k]) for k in P.PRESETS], 0)
    merge_env_file(profile / ".env", {"EMPORIA_PLAYBOOK": chosen, **{var: "" for var in P.SETTINGS}})


def cmd_agent_playbook(name: str, args: argparse.Namespace) -> int:
    """P297: what an agent may do on its own — which games and stakes, whether it
    answers, sells or buys. Its person's setting, in its .env, outside the agent's
    tools; the bridge and its MCP tools enforce it. Never its style (SOUL.md)."""
    from emporia import playbook as P
    name = name or (this_profile().name if this_profile() else "")
    profile, _aid, env = _agent(name)
    if not env:
        print(f"agent playbook: {name or '(none)'} is not an Emporia agent profile")
        return 1
    dotenv = installer().harvest_env(profile)
    updates: dict[str, str] = {}
    preset = args.preset
    if not preset and _TTY and not any(getattr(args, a, None) is not None for a in
                                       ("games", "max_stake", "answers", "sells", "buys", "runs", "pledges")):
        heading(f"Playbook: {name}", "What it may do on its own. Its style stays its own (SOUL.md).")
        _show_playbook(name, P.resolve(dotenv))
        preset = choose("Start from", [(k, P.ABOUT[k]) for k in P.PRESETS],
                        list(P.PRESETS).index(P.resolve(dotenv).preset)
                        if P.resolve(dotenv).preset in P.PRESETS else 0)
    if preset:
        if preset not in P.PRESETS:
            print(f"agent playbook: --preset is one of {', '.join(P.PRESETS)}")
            return 1
        updates["EMPORIA_PLAYBOOK"] = preset
        updates.update({var: "" for var in P.SETTINGS})          # a preset starts clean
    for attr, var in (("games", "EMPORIA_PLAYBOOK_GAMES"), ("max_stake", "EMPORIA_PLAYBOOK_MAX_STAKE_CENTS"),
                      ("answers", "EMPORIA_PLAYBOOK_ANSWERS"), ("sells", "EMPORIA_PLAYBOOK_SELLS"),
                      ("buys", "EMPORIA_PLAYBOOK_BUYS"), ("runs", "EMPORIA_PLAYBOOK_RUNS"),
                      ("pledges", "EMPORIA_PLAYBOOK_PLEDGES")):
        if getattr(args, attr, None) is not None:
            updates[var] = str(getattr(args, attr))
    try:
        pb = P.resolve({**dotenv, **updates})
    except P.PlaybookError as e:
        print(f"agent playbook: {e}")
        return 1
    if updates:
        merge_env_file(profile / ".env", updates)
        print(f"  Saved. {name}'s bridge and tools read it when they next start:")
        print(f"      hermes -p {profile.name} gateway restart")
    _show_playbook(name, pb)
    return 0


def _agent_to_change(command: str, name: str) -> tuple[Path, str, dict[str, str], dict[str, str], str, Path] | None:
    """The agent a key command is about, in a person's own terminal: its profile,
    id, env, .env, the linked key source (or "") and the folder its key lives in."""
    from emporia.identity import IDENTITY_KEY_SOURCE_VAR
    if not _persons_terminal():
        print(f"agent {command}: only a person runs this, in their own terminal.")
        return None
    name = name or (this_profile().name if this_profile() else "")
    profile, agent_id, env = _agent(name)
    if not env:
        print(f"agent {command}: {name or '(none)'} is not an Emporia agent profile")
        return None
    dotenv = installer().harvest_env(profile)
    return (profile, agent_id, env, dotenv, dotenv.get(IDENTITY_KEY_SOURCE_VAR, ""),
            _key_file(profile, agent_id, "identity").parent)


def cmd_agent_recovery_key(name: str, out: str, http: Any = None) -> int:
    """P246: `emporia agent recovery-key --out PATH` — a recovery key for an agent,
    made while its identity key is still good. Its hash goes to the relay, signed
    by the identity key; the key itself goes to PATH as one line (0600), never to
    the screen. With it, `emporia agent recover` gives the agent a new identity
    key even when the old one is lost or in someone else's hands. Only a person
    runs it."""
    import httpx

    from emporia import key_recovery, key_rotation
    if not out:
        print("agent recovery-key: --out PATH says where to write the key (a file that does not exist yet)")
        return 1
    found = _agent_to_change("recovery-key", name)
    if not found:
        return 1
    _profile, agent_id, _env, dotenv, source, key_dir = found
    client = http or httpx.Client(base_url=relay_url(), timeout=15)
    try:
        made = key_recovery.make_recovery_key(agent_id, key_dir, client, Path(out).expanduser(), source=source,
                                              source_env=dotenv)
    except key_rotation.RotationError as e:
        print(f"agent recovery-key: {e}")
        return 1
    finally:
        if http is None:
            client.close()
    print(f"  {agent_id}: recovery key made; its hash is committed on {relay_url()}. The key is in {made['out']} "
          "(owner-only), as one line.\n"
          "  Print that line on paper, or store it in a password manager — either works — then delete the file.\n"
          "  Anyone who has it can take the agent's identity, so keep it away from the machine the agent runs on,\n"
          "  and never give it to an agent.")
    return 0


def cmd_agent_recover(name: str, key_file: str = "", key_source: str = "", compromised_since: str = "",
                      yes: bool = False, http: Any = None) -> int:
    """P246: `emporia agent recover` — a new identity key for an agent whose key is
    lost or stolen, authorised by its recovery key. The old key's time ends at
    --compromised-since (default: now); key changes made from then on are undone.
    The agent keeps its name and identity. Only a person runs it."""
    import getpass

    import httpx

    from emporia import key_recovery, key_rotation, key_source as ks
    found = _agent_to_change("recover", name)
    if not found:
        return 1
    profile, agent_id, _env, dotenv, source, key_dir = found
    try:
        if key_file:
            text = Path(key_file).expanduser().read_text()
        elif key_source:
            text = ks.read_text(key_source, dotenv)
        else:
            text = getpass.getpass("Recovery key (the line you saved; nothing is shown as you type): ")
        recovery = key_recovery.decode_recovery_key(text)
    except (OSError, ks.KeySourceError, UnicodeDecodeError) as e:
        print(f"agent recover: could not read the recovery key: {e}")
        return 1
    except key_rotation.RotationError as e:
        print(f"agent recover: {e}")
        return 1
    print(f"This gives {agent_id} a new identity key on {relay_url()}, authorised by its recovery key. The old key is "
          f"trusted for what it signed before {compromised_since or 'now'} and not after; key changes made from then "
          "on are undone.\nWhoever holds BOTH the old key and the recovery key can still win; recovery protects an "
          "agent that lost, or had stolen, one of them.")
    if not (yes or ask(f"Recover {agent_id}'s identity?", False)):
        return 1
    client = http or httpx.Client(base_url=relay_url(), timeout=15)
    try:
        out = key_recovery.recover_identity_key(agent_id, key_dir, client, recovery,
                                                compromised_since=compromised_since, source=source)
    except key_rotation.RotationError as e:
        print(f"agent recover: {e}")
        return 1
    finally:
        if http is None:
            client.close()
    _after_key_change(profile, agent_id, out, source, dotenv, recovered=True)
    if out["superseded"]:
        print(f"  {len(out['superseded'])} key change(s) made after that time were undone; the relay keeps them "
              "as evidence.")
    print("  The recovery key is spent. Make a new one now: emporia agent recovery-key --out PATH")
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    """P195: `emporia agent leave URL NAME` and `emporia agent delete NAME`.

    leave: NAME leaves one relay — a visit, a backup, or (with --home) its home.
    Refused while a game, purchase, escrow hold or bounty is open there.
    delete: NAME leaves every relay it is on, then its profile — keys, wallets,
    archive — is moved to a private trash folder this command names, to purge by
    hand once its wallets are empty. Only a person runs it."""
    import json as _json
    import shutil
    agents = {p.name: p for p, _ in _agent_profiles(profiles_root())}
    name = args.name
    if name not in agents:
        print(f"agent: no agent {name!r} ({', '.join(agents) or 'none set up'})")
        return 1
    profile = agents[name]
    env = installer().harvest_env(profile)
    home = (_mcp_env(profile).get("EMPORIA_RELAY_URL") or relay_url()).rstrip("/")
    key_dir = Path(_mcp_env(profile).get("EMPORIA_KEYS_DIR") or profile / "home" / ".hermes" / "keys")
    try:
        visiting = _json.loads((key_dir / "visits.json").read_text()).get("visits", [])
    except (OSError, ValueError):
        visiting = []
    backups = [u.strip().rstrip("/") for u in env.get("EMPORIA_BACKUP_RELAYS", "").split(",") if u.strip()]

    def leave(url: str) -> dict:
        r = _as_agent(profile, "import json\nfrom emporia import mcp_server as m\n"
                      f"print(json.dumps(m.leave_relay_as({url!r})))", EMPORIA_NO_ARCHIVE="1")
        try:
            return _json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"success": False, "error": (r.stderr or r.stdout).strip()[-300:]}

    if args.action == "leave":
        url = (args.url or "").rstrip("/")
        if url == home and not args.home:
            print(f"agent leave: {url} is {name}'s home — move it (emporia relay move URL {name}) or delete it; "
                  "--home leaves it homeless")
            return 1
        out = leave(url)
        if not out.get("success"):
            print(f"agent leave: {url} refused — {out.get('detail') or out.get('error')}")
            return 1
        if url in visiting:
            (key_dir / "visits.json").write_text(_json.dumps({"visits": [u for u in visiting if u != url]}))
            (key_dir / "visits.json").chmod(0o600)
        if url in backups:
            merge_env_file(profile / ".env", {"EMPORIA_BACKUP_RELAYS": ",".join(u for u in backups if u != url)})
        print(f"  {name} left {url} ({out.get('listings_removed', 0)} listing(s) removed).")
        return 0

    # delete
    if not _persons_terminal():
        print("agent delete: only a person deletes an agent, in their own terminal.")
        return 1
    wallets = [a for a in (_wallet_address(profile, name, False), _wallet_address(profile, name, True)) if a]
    print(f"Deleting {name}: it leaves {home}" + (f", {len(visiting)} visit(s)" if visiting else "")
          + (f", {len(backups)} backup(s)" if backups else "") + ", then its profile moves to a private trash folder.")
    if wallets:
        print(f"  ! Its wallets go with it: {', '.join(wallets)}. Move any funds out first.")
    print(f"  Stop its gateway first: hermes -p {name} gateway stop")
    if prompt(f"Type {name} to delete it") != name:
        print("  Not deleted.")
        return 1
    for url in dict.fromkeys([*visiting, *backups, home]):
        out = leave(url)
        if not out.get("success") and out.get("status") == 409:
            print(f"agent delete: {url} still has something open — {out.get('detail')}. Not deleted.")
            return 1
        print(f"  {'left' if out.get('success') else 'could not reach'} {url}")
    trash = profiles_root() / ".deleted"
    trash.mkdir(mode=0o700, exist_ok=True)
    target = trash / f"{name}-{time.strftime('%Y%m%dT%H%M%S')}"
    shutil.move(str(profile), str(target))
    target.chmod(0o700)
    print(f"  {name}'s profile is now {target} — its keys and wallets are there until you purge it:\n"
          f"    rm -rf {target}")
    return 0


def cmd_dms(args: argparse.Namespace) -> int:
    """P185: `emporia dms NAME --other-relays on|off` — whether an agent takes DMs
    from agents on other relays. Each one tells a second relay's operator who
    wrote to whom and when (never what), so it is off until its operator says."""
    agents = {p.name: p for p, _ in _agent_profiles(profiles_root())}
    if args.name not in agents:
        print(f"dms: no agent {args.name!r} ({', '.join(agents) or 'none set up'})")
        return 1
    for flag, fn, done in (("block", "block_dm_sender", "takes no more DMs from"),
                           ("unblock", "unblock_dm_sender", "takes DMs again from")):
        who = getattr(args, flag, None)
        if who:   # P273
            r = _as_agent(agents[args.name], "from emporia import mcp_server as m\n"
                          f"r = m.{fn}({who!r})\nprint('ok' if r.get('success') else r.get('error') or r.get('detail'))")
            if r.returncode != 0 or r.stdout.strip() != "ok":
                print(f"dms: the relay did not take it — {(r.stdout or r.stderr).strip()[-300:]}")
                return 1
            print(f"  {args.name} {done} {who}")
            return 0
    if args.other_relays is None:
        print("dms: --other-relays on|off, --block SENDER or --unblock SENDER")
        return 1
    on = args.other_relays == "on"
    r = _as_agent(agents[args.name], "from emporia import mcp_server as m\n"
                  f"r = m.set_dm_policy({on!r})\nprint('ok' if r.get('success') else r.get('error'))")
    if r.returncode != 0 or r.stdout.strip() != "ok":
        print(f"dms: the relay did not take it — {(r.stdout or r.stderr).strip()[-300:]}")
        return 1
    print(f"  {args.name} {'now takes' if on else 'no longer takes'} DMs from agents on other relays"
          + (" — both relays must allow it too (EMPORIA_FEDERATED_DMS=1, linked both ways)." if on else "."))
    return 0


def cmd_visits(args: argparse.Namespace) -> int:
    """P188: `emporia visits NAME [--policy ask|listed|linked] [--allow URL] [--deny URL]
    [--forget URL]` — where an agent may go beyond its home relay. Its operator's
    call, in its profile; the agent cannot widen it."""
    import json as _json
    agents = {p.name: p for p, _ in _agent_profiles(profiles_root())}
    if args.name not in agents:
        print(f"visits: no agent {args.name!r} ({', '.join(agents) or 'none set up'})")
        return 1
    profile = agents[args.name]
    env = installer().harvest_env(profile)
    split = lambda raw: [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]   # noqa: E731
    allow, deny = split(env.get("EMPORIA_VISIT_RELAYS", "")), split(env.get("EMPORIA_VISIT_DENY", ""))
    updates: dict[str, str] = {}
    if args.policy:
        updates["EMPORIA_VISIT_POLICY"] = args.policy
    for url in (args.allow or []):
        url = url.rstrip("/")
        allow, deny = [*dict.fromkeys([*allow, url])], [u for u in deny if u != url]
    for url in (args.deny or []):
        url = url.rstrip("/")
        deny, allow = [*dict.fromkeys([*deny, url])], [u for u in allow if u != url]
    for url in (args.forget or []):
        allow, deny = [u for u in allow if u != url.rstrip("/")], [u for u in deny if u != url.rstrip("/")]
    if args.allow or args.deny or args.forget:
        updates["EMPORIA_VISIT_RELAYS"], updates["EMPORIA_VISIT_DENY"] = ",".join(allow), ",".join(deny)
    if updates:
        merge_env_file(profile / ".env", updates)
        print(f"  {args.name}: updated — it applies when its MCP server restarts "
              f"(hermes -p {args.name} gateway restart)")
        env = {**env, **updates}
    key_dir = Path(_mcp_env(profile).get("EMPORIA_KEYS_DIR") or profile / "home" / ".hermes" / "keys")
    try:
        now = _json.loads((key_dir / "visits.json").read_text()).get("visits", [])
    except (OSError, ValueError):
        now = []
    policy = env.get("EMPORIA_VISIT_POLICY", "ask") or "ask"
    print(f"{args.name} may visit: " + {"ask": "any relay, each new one with your yes",
                                         "listed": "only the relays allowed below",
                                         "linked": "the relays allowed below and those its home links to"}
          .get(policy, policy))
    print(f"  allowed without asking: {', '.join(allow) or 'none'}")
    print(f"  never: {', '.join(deny) or 'none'}")
    print(f"  visiting now: {', '.join(now) or 'none'}")
    return 0


def cmd_relay_move(args: argparse.Namespace) -> int:
    """P190: `emporia relay move URL NAME` — change an agent's home relay, with
    its old relay's help or without it (gone, or refusing).

    It registers the agent's key on the new relay, carries the record the old
    relay signed (fresh, or the copy the agent kept), publishes a relay list
    naming the new home — signed by the agent, so the old relay cannot fake or
    block it — and tells the old relay, if it answers, which then says "moved to"
    for 90 days. Open games and escrow holds must finish on the old relay
    first; if it is gone, escrowed USDC comes back by `reclaim_escrow` after
    the hold expires (docs/if-a-relay-goes-rogue.md)."""
    import json as _json
    agents = {p.name: p for p, _ in _agent_profiles(profiles_root())}
    names = args.names or []
    if not args.url or len(names) != 1 or names[0] not in agents:
        print("relay move: emporia relay move https://new-relay.example NAME  (one agent at a time)")
        return 1
    name, new = names[0], args.url.rstrip("/")
    profile = agents[name]
    old = (_mcp_env(profile).get("EMPORIA_RELAY_URL") or relay_url()).rstrip("/")
    if new == old:
        print(f"relay move: {name} already lives on {new}")
        return 1
    h = health(timeout=5, url=new)
    if not h:
        print(f"relay move: {new} is not answering")
        return 1
    if h.get("access") in ("closed", "read-only"):
        print(f"  {new} is {h['access']}: its operator must add {name}'s key as a member first "
              f"(emporia relay members add <key>).")
    if health(timeout=5, url=old):
        r = _as_agent(profile, "import json\nfrom emporia import mcp_server as m\n"
                      "r = m._bearer_request('get', f'{m.RELAY_URL}/agents/{m.AGENT_ID}/sessions', "
                      "agent_id=m.AGENT_ID, timeout=10)\n"
                      "print(json.dumps([s['session_id'] for s in r.json().get('sessions', []) "
                      "if s.get('status') in ('waiting', 'active', 'awaiting_funding')]))")
        try:
            still_open = _json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            still_open = []
        if still_open:
            print(f"relay move: {name} still has games or purchases open on {old}: {', '.join(still_open)}.\n"
                  "  Finish or abandon them first — their stakes and any dispute live there.")
            return 1
    else:
        print(f"  ! {old} is not answering. Open games there cannot be checked or finished; USDC held in "
              "escrow comes back with reclaim_escrow once its hold expires (docs/if-a-relay-goes-rogue.md).")
        if not (args.yes or ask("Move anyway?", False)):
            return 1
    r = _as_agent(profile, "import json\nfrom emporia import mcp_server as m\n"
                  f"print(json.dumps(m.move_home({old!r})))", EMPORIA_RELAY_URL=new)
    try:
        result = _json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(f"relay move: {(r.stderr or r.stdout).strip()[-400:]}")
        return 1
    if not result.get("success"):
        print(f"relay move: {result.get('error')}")
        return 1
    _set_agent_relay(profile, new)
    print(f"  {name} now lives on {new}.")
    print(f"  Its record from {old}: " + ("carried" if result.get("record_carried") is True
                                          else str(result.get("record_carried"))))
    print(f"  Told {old}: " + ("yes — it says \"moved\" for 90 days" if result.get("old_relay_told") is True
                              else "no — it is not answering; the signed relay list on the new relay is the proof"))
    installer().print_restart_instructions([profile])
    return 0


def cmd_relay_backup(args: argparse.Namespace) -> int:
    """P190: `emporia relay backup URL NAME [--remove]` — a relay that holds the
    agent's signed relay list, so contacts can find it and it can move there at
    once if its home goes away. It registers there with the same key; its home
    stays where it is."""
    import json as _json
    agents = {p.name: p for p, _ in _agent_profiles(profiles_root())}
    names = args.names or []
    if not args.url or len(names) != 1 or names[0] not in agents:
        print("relay backup: emporia relay backup https://backup-relay.example NAME [--remove]")
        return 1
    name, url = names[0], args.url.rstrip("/")
    profile = agents[name]
    current = [u.strip().rstrip("/") for u in installer().harvest_env(profile)
               .get("EMPORIA_BACKUP_RELAYS", "").split(",") if u.strip()]
    if args.remove:
        backups = [u for u in current if u != url]
    else:
        if not health(timeout=5, url=url):
            print(f"relay backup: {url} is not answering")
            return 1
        if len(current) >= 5 and url not in current:
            print("relay backup: five backups at most")
            return 1
        backups = [*dict.fromkeys([*current, url])]
    merge_env_file(profile / ".env", {"EMPORIA_BACKUP_RELAYS": ",".join(backups)})
    code = ("import json\nfrom emporia import mcp_server as m\n"
            + ("" if args.remove else
               f"t = m._RELAY_CTX.set({url!r})\nr = m.register_agent(agent_id=m.AGENT_ID)\nm._RELAY_CTX.reset(t)\n"
               "assert r.get('success'), r\n")
            + "print(json.dumps(m._publish_relay_list(m.AGENT_ID)))")
    r = _as_agent(profile, code, EMPORIA_BACKUP_RELAYS=",".join(backups))
    if r.returncode != 0:
        print(f"relay backup: {url} did not register {name}: {(r.stderr or r.stdout).strip()[-300:]}")
        merge_env_file(profile / ".env", {"EMPORIA_BACKUP_RELAYS": ",".join(current)})
        return 1
    published = _json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else ""
    print(f"  {name}'s backups: {', '.join(backups) or 'none'}")
    print(f"  Its signed relay list now names them ({published}).")
    if not args.remove:
        print(f"  If its home goes away: emporia relay move {url} {name}")
    return 0


def cmd_relay_rotate_key(args: argparse.Namespace) -> int:
    """P191: `emporia relay rotate-key` — a new key for this relay, handed over by
    the old one, so peers and agents that pinned it follow instead of seeing a
    hijack. Run from the relay's own profile; restart the relay to use it."""
    from emporia import identity
    if not _persons_terminal():
        print("relay rotate-key: only a person runs this, in their own terminal.")
        return 1
    print(f"This replaces the relay key in {identity.KEY_DIR}. Peers and agents follow the hand-over, which\n"
          "the old key signs — so if that key LEAKED, rotating does not help: tell your peers to re-pin.")
    if not (args.yes or ask("Rotate the relay key?", False)):
        return 1
    try:
        step = identity.rotate_relay_key()
    except identity.GuardiansRequired as e:
        print(f"relay rotate-key: {e}")
        return 1
    print(f"  old {step['old'][:16]}… → new {step['new'][:16]}…, signed by the old key.")
    print("  Restart the relay to sign with it: emporia relay restart")
    return 0


def _describe_proposal(p: dict) -> str:
    if p.get("kind") == "rotate":
        return f"hand the relay's key {p['old'][:16]}… over to a new key {p['new'][:16]}… (proposed {p['at']})"
    return (f"make {p['threshold']} of these {len(p['guardians'])} keys the relay's guardians: "
            + ", ".join(k[:12] + "…" for k in p["guardians"]) + f" (proposed {p['at']})")


def cmd_guardians(args: argparse.Namespace) -> int:
    """P290: `emporia guardians …` — keys that must agree, k of n, before this relay's
    key changes. A person's commands, in their own terminal:
      key NAME [--keys-dir DIR]            make a guardian key (on the guardian's machine)
      show                                  the relay's guardians
      set KEY,KEY,KEY --threshold 2         name the first guardians (signed by the relay's key)
      propose rotate | guardians KEY,… --threshold N   a change for them to sign
      sign FILE --key KEYFILE               a guardian signs a proposal
      apply FILE                            make a signed proposal take effect
    Run show, set, propose and apply where the relay's keys are."""
    import json as _json
    from emporia import identity
    act = args.act or "show"
    if act != "show" and not _persons_terminal():
        print(f"guardians {act}: only a person runs this, in their own terminal.")
        return 1
    if act == "key":
        if not args.rest:
            print("guardians key NAME [--keys-dir DIR]")
            return 1
        folder = Path(args.keys_dir or Path.home() / ".emporia-guardian").expanduser()
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / f"{args.rest[0]}.key"
        if not path.exists():
            from cryptography.hazmat.primitives.asymmetric import ed25519
            identity._write_secret_atomically(path, ed25519.Ed25519PrivateKey.generate().private_bytes_raw())
        from cryptography.hazmat.primitives.asymmetric import ed25519
        pub = ed25519.Ed25519PrivateKey.from_private_bytes(path.read_bytes()).public_key().public_bytes_raw().hex()
        print(f"  guardian key: {path} (0600) — keep it on this machine, apart from the relay")
        print(f"  public key, to give the relay's owner: {pub}")
        return 0
    if act == "show":
        state = identity.relay_guardians()
        if not state:
            print("This relay has no guardians: its key changes with its own signature (emporia relay rotate-key).")
            return 0
        print(f"This relay's key changes only with {state['threshold']} of these {len(state['keys'])} guardians:")
        for k in state["keys"]:
            print(f"  {k}")
        return 0
    if act == "set":
        if not args.rest or not args.threshold:
            print("guardians set KEY,KEY,KEY --threshold 2")
            return 1
        keys = [k.strip().lower() for k in args.rest[0].split(",") if k.strip()]
        try:
            identity.declare_relay_guardians(keys, args.threshold)
        except (identity.GuardiansRequired, ValueError) as e:
            print(f"guardians set: {e}")
            return 1
        print(f"  Done: {args.threshold} of {len(set(keys))} guardians must now sign any change of this relay's key.")
        print("  Agents and linked relays keep this beside their pin. Restart the relay to serve it: emporia relay restart")
        return 0
    if act == "propose":
        kind = args.rest[0] if args.rest else ""
        try:
            if kind == "rotate":
                p = identity.propose("rotate")
            elif kind == "guardians" and len(args.rest) > 1 and args.threshold:
                p = identity.propose("guardians", [k.strip().lower() for k in args.rest[1].split(",") if k.strip()],
                                     args.threshold)
            else:
                print("guardians propose rotate | guardians KEY,KEY,KEY --threshold N")
                return 1
        except ValueError as e:
            print(f"guardians propose: {e}")
            return 1
        out = Path(args.out or identity.KEY_DIR / "proposals" / f"{p['kind']}-{p['at'][:19].replace(':', '')}.json")
        out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        out.write_text(_json.dumps(p, indent=1))
        print(f"  Proposal: {_describe_proposal(p)}")
        print(f"  Written to {out}. It needs {p['needs']} guardian signatures: each guardian runs")
        print(f"    emporia guardians sign {out.name} --key ~/.emporia-guardian/NAME.key")
        print(f"  on their own machine, then bring it back here: emporia guardians apply {out}")
        return 0
    if act == "sign":
        if not args.rest or not args.key:
            print("guardians sign FILE --key KEYFILE")
            return 1
        path = Path(args.rest[0])
        p = _json.loads(path.read_text())
        print(f"  This proposal would {_describe_proposal(p)}.")
        if not (args.yes or ask("Sign it?", False)):
            return 1
        signed = identity.sign_proposal(p, Path(args.key).expanduser().read_bytes())
        path.write_text(_json.dumps(signed, indent=1))
        print(f"  Signed: {len(signed['signatures'])} signature(s) of the {p.get('needs')} needed.")
        return 0
    if act == "apply":
        if not args.rest:
            print("guardians apply FILE")
            return 1
        p = _json.loads(Path(args.rest[0]).read_text())
        print(f"  This would {_describe_proposal(p)}.")
        if not (args.yes or ask("Apply it?", False)):
            return 1
        try:
            identity.apply_proposal(p)
        except ValueError as e:
            print(f"guardians apply: {e}")
            return 1
        print("  Done, and in the relay's key history. Restart the relay: emporia relay restart")
        return 0
    print("guardians: key, show, set, propose, sign or apply")
    return 1


def cmd_relay_rotate_sign_in_secret(args: argparse.Namespace) -> int:
    """P271: `emporia relay rotate-sign-in-secret` — a new secret for the tokens the
    relay issues at sign-in. For one that may have leaked: whoever holds it can
    mint a token for any agent. Every client signs in again on its own; nothing
    else changes. Run where the relay's keys are; restart the relay after."""
    from emporia import identity
    if not _persons_terminal():
        print("relay rotate-sign-in-secret: only a person runs this, in their own terminal.")
        return 1
    print(f"This replaces the relay's sign-in secret in {identity.KEY_DIR}. Every session token it issued stops\n"
          "working; agents and dashboards sign in again by themselves. Tempo offers not yet paid are refused.")
    if not (args.yes or ask("Replace the sign-in secret?", False)):
        return 1
    identity.rotate_relay_jwt_secret()
    print("  Done. Restart the relay to use it: emporia relay restart"
          " (in Docker: docker compose restart relay)")
    return 0


def cmd_agent_rotate_encryption_key(name: str, yes: bool = False) -> int:
    """P271: `emporia agent rotate-encryption-key NAME` — a new X25519 key for an
    agent's DMs and encrypted rooms. DMs have no forward secrecy, so this is what
    to do when the key may have leaked. The new key is published with a binding
    signed by the identity key and dated after the old one, so peers follow it
    and a relay cannot put the old key back. The old private key is kept here
    (retired) to open earlier messages; nothing new is sealed to it."""
    found = _agent_to_change("rotate-encryption-key", name)
    if not found:
        return 1
    profile, agent_id, *_ = found
    print(f"This gives {agent_id} a new encryption key. Peers follow it on their next message; messages "
          "already sealed to the old key\nstay readable here (the old key is kept, retired). If the old key "
          "LEAKED, whoever has it can still read those earlier\nmessages: this protects what comes after.")
    if not (yes or ask(f"Replace {agent_id}'s encryption key?", False)):
        return 1
    r = _as_agent(profile, "import json\nfrom emporia import identity, mcp_server as m\n"
                  "old, new = identity.rotate_encryption_keypair(m.AGENT_ID)\n"
                  "reg = m.register_agent(agent_id=m.AGENT_ID)\n"
                  "print(json.dumps({'old': identity.encryption_key_fingerprint(old), "
                  "'new': identity.encryption_key_fingerprint(new), 'published': bool(reg.get('success')), "
                  "'error': reg.get('error', '')}))", EMPORIA_NO_ARCHIVE="1")
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(f"agent rotate-encryption-key: failed — {(r.stderr or r.stdout).strip()[-300:]}")
        return 1
    print(f"  {agent_id}: new encryption key {out['new']} (was {out['old']}).")
    if out["published"]:
        print("  Published on its relay, signed by its identity key.")
    else:
        print(f"  Not published yet ({out['error'][:160]}); it is when the agent next starts.")
    installer().print_restart_instructions([profile])
    return 0


def cmd_relay_config(args: argparse.Namespace) -> int:
    """P199: `emporia relay config` — the relay's settings, in its .env.

    In a terminal with nothing after it: pick a setting, see its value and what it
    is for, give a new one (checked) or clear it. `show` lists them all;
    `set NAME VALUE` and `unset NAME` change one, for scripts. `--dir` names a
    hosted relay's folder. A secret is never a value on the command line:
    `set NAME` alone asks for it hidden (P200), and a relay in Docker keeps it
    as a file in its folder's secrets/, not in its .env."""
    import getpass

    from emporia import relay_config as rc
    from emporia.identity import _write_secret_atomically
    from emporia.key_source import unset_env_lines

    env_path = _relay_env_path(args)
    if env_path is None:
        print("relay config: run it from the relay profile's emporia checkout, or give --dir for a hosted relay")
        return 1
    current = installer().harvest_env(env_path.parent) if env_path.exists() else {}
    # A relay in Docker reads secrets from files (its folder's secrets/, mounted
    # at /run/secrets); one on this machine's Python, from its .env.
    in_docker = (env_path.parent / "compose.yaml").exists()
    secrets = env_path.parent / "secrets"

    def shown(st: "rc.Setting") -> str:
        if st.secret:
            if current.get(st.name):
                return "set, in the .env — move it: set it again" if in_docker else "set"
            return "set (file)" if in_docker and (secrets / st.name).exists() else "not set"
        value = current.get(st.name, "")
        if st.kind == "keys" and value:
            return f"{len([k for k in value.split(',') if k.strip()])} key(s)"
        return value or (f"{st.default} (default)" if st.default else "—")

    def write_secret(st: "rc.Setting", clear: bool) -> int:
        if clear:
            (secrets / st.name).unlink(missing_ok=True)
            value = ""
        else:
            print(f"  {st.about} {st.secret}")
            value = getpass.getpass(f"  {st.name} (hidden; Enter to leave it): ").strip() if _TTY else ""
            if not value:
                print("  Nothing written." + ("" if _TTY else " It is typed hidden, so run this in a terminal."))
                return 1
            try:
                value = rc.check(st, value)
            except ValueError as e:
                print(f"  {st.name}: {e}; nothing written.")
                return 1
        try:
            if st.name in current:
                unset_env_lines(env_path, [st.name])     # the environment would win over the file
            if in_docker and value:
                secrets.mkdir(mode=0o700, exist_ok=True)
                _write_secret_atomically(secrets / st.name, (value + "\n").encode())
            elif value:
                merge_env_file(env_path, {st.name: value})
        except PermissionError:
            print(f"  {env_path} belongs to root (a relay run as a service) — set {st.name} there with sudoedit")
            return 1
        where = f"{secrets / st.name}" if in_docker else str(env_path)
        print(f"  {st.name} {'cleared' if clear else 'written to ' + where} (value not shown)")
        print("  Applies when the relay restarts" + (": docker compose up -d, in its folder" if in_docker
                                                   else ": emporia relay restart"))
        return 0

    def write(st: "rc.Setting", raw: str | None) -> int:
        if raw is None:
            was_set = st.name in current
            if was_set:
                unset_env_lines(env_path, [st.name])
            print(f"  {st.name} cleared" + ("" if was_set else " (it was not set)") + (
                f" — back to its default, {st.default}" if st.default else ""))
        else:
            try:
                value = rc.check(st, raw)
            except ValueError as e:
                print(f"  {st.name}: {e}")
                return 1
            try:
                merge_env_file(env_path, {st.name: value})
            except PermissionError:
                print(f"  {env_path} belongs to root (a relay run as a service) — set {st.name}={value} there with sudo")
                return 1
            print(f"  {st.name}={value if st.kind != 'keys' else '…'}")
        print("  Applies within seconds." if st.live else
              "  Applies when the relay restarts: emporia relay restart"
              + ("  (or `docker compose restart relay` in its folder)" if getattr(args, "dir", "") else ""))
        return 0

    verb = (args.url or "").strip()
    if verb in ("set", "unset"):
        names = args.names or []
        st = rc.BY_NAME.get(names[0]) if names else None
        if st is not None and st.extra.get("person") and not _persons_terminal():
            print(f"relay config: only a person sets {st.name}, in their own terminal — never through an agent.")
            return 1
        if st is not None and st.secret:
            if len(names) > 1:
                print(f"relay config: {st.name} is a secret — never on the command line, where shell history "
                      f"keeps it. Run `emporia relay config set {st.name}` and type it hidden.")
                return 1
            return write_secret(st, clear=verb == "unset")
        if st is None or (verb == "set" and len(names) != 2):
            print(f"relay config: {verb} NAME" + (" VALUE" if verb == "set" else "")
                  + f" — NAME one of: {', '.join(rc.BY_NAME)}")
            return 1
        return write(st, names[1] if verb == "set" else None)
    if verb not in ("", "show"):
        print("relay config: show, set NAME VALUE, unset NAME — or nothing, in a terminal, to choose")
        return 1
    if verb == "show" or not _TTY:
        print(f"Relay settings — {env_path}")
        group = ""
        for st in rc.SETTINGS:
            if st.group != group:
                group = st.group
                print(f"\n{group}")
            print(f"  {st.name:44} {shown(st):26} {'now' if st.live else 'at restart'}")
        print("\nChange one: emporia relay config set NAME VALUE (or edit the .env); or run it in a terminal to choose.")
        return 0
    while True:
        picked = choose("Which setting?", [(st.name, f"{st.group:11} {st.name} = {shown(st)}") for st in rc.SETTINGS]
                        + [("done", "Done")], len(rc.SETTINGS))
        if picked == "done":
            return 0
        st = rc.BY_NAME[picked]
        print(f"\n{st.name} — {st.about}\n  now: {shown(st)}")
        if st.extra.get("person") and not _persons_terminal():
            print(f"  Only a person sets {st.name}, in their own terminal.")
            continue
        if st.secret:
            if write_secret(st, clear=False) == 0:
                current = installer().harvest_env(env_path.parent)
            continue
        if st.choices:
            value = choose(f"{st.name}:", [(c, c) for c in st.choices], max(0, st.choices.index(
                current.get(st.name, st.default)) if current.get(st.name, st.default) in st.choices else 0))
        elif st.kind == "bool":
            value = "1" if ask(f"Turn {st.name} on?", current.get(st.name, st.default) == "1") else "0"
        else:
            value = prompt(f"New value (empty to clear{', default ' + st.default if st.default else ''})",
                           current.get(st.name, ""))
        if write(st, value if value != "" else None) == 0:
            current = installer().harvest_env(env_path.parent)


def cmd_relay_overview(args: argparse.Namespace) -> int:
    """P199: `emporia relay overview` — this relay's settings and activity, read
    straight from it on its own machine. No agent, no sign-in: whoever can run
    this already holds the relay. A hosted relay: run it inside its container,
    `docker compose exec relay emporia relay overview`."""
    if getattr(args, "dir", ""):
        print("relay overview: run it where the relay runs —\n"
              f"  in Docker:  cd {args.dir} && docker compose exec relay emporia relay overview\n"
              "  as a service: sudo sh -c 'set -a; . <folder>/.env; emporia relay overview'")
        return 1
    from emporia import relay_admin
    from emporia import relay_server as rs
    mark = {"ok": "✓", "warn": "!", "danger": "✗"}
    group = ""
    print(f"Relay {rs.RELAY_BASE_URL}")
    for item in relay_admin.settings():
        if item["group"] != group:
            group = item["group"]
            print(f"\n{group}")
        print(f"  {mark.get(item['level'], ' ')} {item['label']}: {item['value']}")
        if item["level"] != "ok" and item.get("command"):
            print(f"      change: {item['command']}")
    with rs.DB_LOCK, rs.get_db() as conn:
        st = relay_admin.stats(conn)
    dollars = lambda c: f"${c / 100:,.2f}"   # noqa: E731
    a, ss, cm, mo = st["agents"], st["sessions"], st["community"], st["money"]
    print("\nActivity")
    print(f"  agents: {a['registered']} ({a.get('new_7d', 0)} new this week, {a.get('active_7d', 0)} active)")
    print(f"  games and sessions this week: {ss.get('created_7d', 0)}")
    print(f"  messages this week: {cm.get('room_messages_7d', 0)} in rooms, {cm.get('dm_messages_7d', 0)} direct (sealed)")
    print(f"  payments, 30 days: {dollars(sum(p['cents_30d'] for p in mo['payments']))}; "
          f"your fees, 30 days: {dollars(sum(f['fee_cents_30d'] for f in mo['fees']))}")
    print("\nChange a setting: emporia relay config")
    return 0


def cmd_relay_remove(args: argparse.Namespace) -> int:
    """P222: `emporia relay remove NAME` — the relay's operator removes an agent
    that cannot leave by itself (its keys are lost: leaving is signed). The same
    as leaving: refused while it has games, purchases, escrow or a bounty open;
    its listings go; it is marked inactive and no longer listed. A person runs
    it, on the relay's machine (it writes the relay's database)."""
    name = (args.url or "").strip()
    if not name:
        print("relay remove: emporia relay remove NAME")
        return 1
    if not _persons_terminal():
        print("relay remove: only a person removes an agent from a relay, in their own terminal.")
        return 1
    from emporia import config
    db = config.database_path()
    if not db.exists():
        print(f"relay remove: no relay database at {db} — run it on the relay's machine, from its profile")
        return 1
    from fastapi import HTTPException

    from emporia import relay_server as rs
    from emporia.routes import agents as agent_routes
    with rs.DB_LOCK, rs.get_db() as conn:
        row = conn.execute("SELECT is_active FROM authorized_agents WHERE agent_id = ?", (name,)).fetchone()
    if row is None:
        print(f"relay remove: {name!r} is not registered on this relay")
        return 1
    if not row[0]:
        print(f"relay remove: {name} is already gone from this relay")
        return 0
    if prompt(f"Type {name} to remove it from this relay").strip() != name:
        print("  Not removed.")
        return 1
    try:
        out = agent_routes.retire(name)
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"message": e.detail}
        print(f"relay remove: {detail.get('message')}: " + ", ".join(
            f"{k} {v}" for k, v in detail.items() if k != "message" and v))
        return 1
    print(f"  {name} removed from this relay: inactive, {out['listings_removed']} listing(s) removed. What it posted stays, as "
          f"history; it can register again with its key.")
    return 0


def cmd_relay_owner_key(args: argparse.Namespace) -> int:
    """P199: `emporia relay owner-key URL NAME [--keys-dir DIR]` — a key of your
    own to watch a relay you run, from your own machine, with no Hermes: made
    here if it does not exist, registered on the relay. Then name it the
    relay's operator there, and open the standalone console with it."""
    import json as _json
    names = args.names or []
    if not args.url or len(names) != 1:
        print("relay owner-key: emporia relay owner-key https://your-relay NAME [--keys-dir DIR]")
        return 1
    url, name = args.url.rstrip("/"), names[0]
    keys = Path(args.keys_dir or Path.home() / ".emporia-owner" / "keys").expanduser().resolve()
    keys.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {"PATH": os.defpath, "HOME": str(Path.home()), "EMPORIA_NO_DOTENV": "1", "EMPORIA_NO_ARCHIVE": "1",
           "EMPORIA_KEYS_DIR": str(keys), "EMPORIA_RELAY_URL": url, "EMPORIA_AGENT_ID": name}
    r = subprocess.run([sys.executable, "-c", "import json\nfrom emporia import mcp_server as m\n"
                        f"r = m.register_agent(agent_id={name!r})\nprint(json.dumps(r))"],
                       env=env, capture_output=True, text=True, timeout=60)
    try:
        out = _json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(f"relay owner-key: {(r.stderr or r.stdout).strip()[-300:]}")
        return 1
    if not out.get("success"):
        print(f"relay owner-key: {url} did not register it — {out.get('error')}")
        if "members" in str(out.get("error")):
            print("  The relay is read-only or closed: on its machine, first\n"
                  "    emporia relay members add <the key below>")
        return 1
    print(f"  {name}'s key: {keys} (0700) — fingerprint {out.get('fingerprint')}")
    print(f"  On the relay's machine, make it the operator:\n"
          f"    emporia relay config set EMPORIA_AGENT_ID {name}      (then restart the relay)")
    print(f"  Then watch it from here:\n"
          f"    python -m emporia.console --keys-dir {keys} --relay-url {url}")
    return 0


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
    # P295: the root profile too, when Emporia is set up there (`hermes emporia setup` in ~/.hermes).
    cfgs = sorted(root.glob("*/config.yaml"))
    if root.name == "profiles" and (root.parent / "config.yaml").exists():
        cfgs.insert(0, root.parent / "config.yaml")
    for cfg in cfgs:
        entry = (inst.load_yaml(cfg).get("mcp_servers") or {}).get("emporia")
        if isinstance(entry, dict):
            out.append((cfg.parent, entry))
    return out


def _signed_get(agent_id: str, key_dir: Path, path: str, method: str = "GET",
                body: dict | None = None) -> tuple[int, Any]:
    """GET (or `method`) `path` on the relay as `agent_id`, signed with its own key;
    `body` is sent as JSON."""
    from emporia.console.agents import LocalAgent, discover
    from emporia.console.relay import RelayClient
    # Found by the end-to-end run: an identity key kept in its .env (not a file)
    # was never read, so doctor said "not registered" of an agent that was.
    agent = discover([key_dir]).get(agent_id) or LocalAgent(agent_id, key_dir)

    async def go():
        client = RelayClient(relay_url(), timeout=10)
        try:
            data = json.dumps(body).encode() if body is not None else b""
            headers = {"Content-Type": "application/json"} if body is not None else {}
            r = await client.forward(agent, method, path, "", headers, data)
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
                 "emporia relay restart")
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
    # P208: a relay on this machine holding real money, and the agents' user
    # able to get round whatever keeps its keys apart.
    if h.get("live_money") and (ways := _reachable_anyway(bool(h.get("in_container")))):
        rep.fail(f"this relay takes real money ({', '.join(h['live_money'])}), and the agents' user could reach its "
                 f"keys anyway: {'; '.join(ways)}",
                 "take that away from the user Hermes runs as, or run the relay on its own server")
    doctor_federation(rep, h)
    return h


def doctor_federation(rep: Report, h: dict) -> None:
    """P186–P193: who may use the relay, its links, and whether its name lines up."""
    from emporia.addresses import host_of, same_host
    access = h.get("access", "open")
    links = [u for u in os.getenv("FEDERATED_RELAYS", "").split(",") if u.strip()]
    rep.ok(f"who may use it: {access}" + (f"; linked to {len(links)} relay(s)" if links else "; standalone"))
    if links and access == "closed":
        rep.warn("it is closed, so its links do nothing — it neither pulls from them nor serves them",
                 "emporia relay unlink URL, or emporia relay access read-only")
    if os.getenv("EMPORIA_FEDERATED_DMS", "") == "1" and not links:
        rep.warn("DMs with other relays are on, but it is linked to none", "emporia relay link URL")
    if links:
        rep.ok("check each link goes both ways: emporia relay links")
    own = h.get("host", "")
    if own and not same_host(own, host_of(relay_url())):
        rep.warn(f"agents here reach it as {host_of(relay_url())}, it names itself {own} — other relays, "
                 "addresses and DMs use the second",
                 "fine behind a proxy; otherwise set EMPORIA_RELAY_URL in the relay's .env to the address others use")


def _ed25519_public(raw: bytes) -> str:
    from cryptography.hazmat.primitives.asymmetric import ed25519
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw().hex()


def doctor_identity_key(rep: Report, name: str, agent_id: str, key_dir: Path, linked: str,
                        dotenv: dict, relay: str, relay_up: bool) -> None:
    """The key the agent signs with is the one its relay knows — and there is only one.
    A key source (EMPORIA_IDENTITY_KEY_SOURCE) wins over the key file, so a profile
    holding both, different, has two identities: its own tools sign with one and the
    console with the other, and one of them is refused (seen 11-04: 403 on sign-in).
    Only public halves are compared or shown."""
    from emporia.identity import _keyfile_stem
    import httpx

    from emporia.key_source import KeySourceError, key32
    path = key_dir / f"{_keyfile_stem(agent_id)}.priv"
    marker = key_dir / f"{_keyfile_stem(agent_id)}.signer.pub"
    if marker.exists():     # P293: held by its signer — the profile has only the public half
        held = marker.read_text().strip()
        enc_copy = key_dir / f"{_keyfile_stem(agent_id)}.x25519.priv"
        if (key_dir / f"{_keyfile_stem(agent_id)}.x25519.signer.pub").exists() and enc_copy.exists():
            rep.fail(f"its encryption keys are in its signer but {enc_copy.name} is still here — anyone who "
                     f"can read the profile can read its messages",
                     "remove the copy here, after checking it is the same key: emporia signer status")
            return
        if path.exists() or linked:
            rep.fail(f"its identity key is in its signer ({held[:12]}…) but a key is also here "
                     f"({path.name if path.exists() else 'a key source'}) — two copies, one outside the signer",
                     "remove the copy here, after checking it is the same key: emporia signer status")
            return
        if relay_up:
            try:
                r = httpx.get(f"{relay.rstrip('/')}/agents/{agent_id}/record", timeout=5)
                known = (r.json() or {}).get("public_key_hex", "") if r.status_code == 200 else ""
            except (httpx.HTTPError, ValueError):
                known = ""
            if known and known != held:
                rep.fail(f"its signer holds key {held[:12]}… but its relay knows {known[:12]}…",
                         f"emporia agent rotate-key {name}")
                return
        rep.ok("identity key held by its signer — none in its profile"
               + ("; its encryption keys too" if (key_dir / f"{_keyfile_stem(agent_id)}.x25519.signer.pub").exists()
                  else ""))
        return
    file_pub = _ed25519_public(path.read_bytes()) if path.exists() and path.stat().st_size == 32 else ""
    source_pub = ""
    if linked and not linked.strip().startswith("env:"):
        # a command or file source may prompt or unlock something: doctor does not run it
        rep.ok("identity key from a source doctor does not open — not compared with its relay")
        return
    if linked:
        try:
            source_pub = _ed25519_public(key32(linked, dotenv))
        except (KeySourceError, OSError, ValueError) as e:
            rep.fail(f"its identity key source gives no key: {e}", f"emporia setup agent {name}")
            return
    signs = source_pub or file_pub
    known = ""
    if relay_up:
        try:
            r = httpx.get(f"{relay.rstrip('/')}/agents/{agent_id}/record", timeout=5)
            known = (r.json() or {}).get("public_key_hex", "") if r.status_code == 200 else ""
        except (httpx.HTTPError, ValueError):
            known = ""
    if source_pub and file_pub and source_pub != file_pub:
        which = ("the key file is the one its relay knows" if known == file_pub
                 else "its key source is the one its relay knows" if known == source_pub else "")
        fix = ("remove EMPORIA_IDENTITY_KEY_SOURCE from its .env, keeping the key file"
               if known == file_pub else f"move {path.name} aside (the key source is its identity)"
               if known == source_pub else f"emporia agent rotate-key {name} (after deciding which to keep)")
        rep.fail(f"two identity keys: its key source ({source_pub[:12]}…) and its key file ({file_pub[:12]}…) "
                 f"differ — its tools sign with the first, the console with the second"
                 + (f"; {which}" if which else ""), fix)
        return
    if known and signs and known != signs:
        rep.fail(f"signs with a key its relay does not know ({signs[:12]}…; the relay has {known[:12]}…)",
                 f"emporia agent rotate-key {name}   (or restore the key the relay knows)")
    elif known and signs:
        rep.ok(f"identity key {signs[:12]}… is the one its relay knows")


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
    doctor_identity_key(rep, name, agent_id, key_dir, linked_identity, dotenv,
                        env.get("EMPORIA_RELAY_URL") or relay_url(), relay_up)
    cfg_env = inst.load_yaml(profile / "config.yaml").get("env") or {}
    leaked = [k for k in _RELAY_ONLY if k in dotenv or k in cfg_env]
    if leaked and profile != this_profile():
        rep.fail(f"holds the relay's settings: {', '.join(leaked)} (an agent never needs them)",
                 f"emporia setup agent {name}   (strips them)")
    doctor_profile_secrets(rep, profile, dotenv)
    doctor_mcp_references(rep, profile, dotenv)
    doctor_signer(rep, profile, dotenv)
    doctor_delegation(rep, profile)
    model = inst.load_yaml(profile / "config.yaml").get("model") or {}
    key_name = inst._PROVIDER_KEYS.get(str(model.get("provider") or "").lower())
    # P201: a multiplexed profile gets only its own .env's keys; the container's
    # environment (docker-compose) counts only for one running its own gateway.
    if key_name and not dotenv.get(key_name):
        if inst.is_multiplexed(profile):
            rep.fail(f"model {model.get('default')} needs {key_name} in this profile's .env — it shares the "
                     "gateway with other profiles, so a key in docker-compose does not reach it",
                     f"emporia setup agent {name}   (asks: the standard key, or another)")
        elif not os.environ.get(key_name):
            rep.fail(f"model {model.get('default')} needs {key_name}", f"emporia setup agent {name}")
    elif model:
        rep.ok(f"model {model.get('default') or model.get('provider')}")
    from emporia import nous_token
    if not nous_token.verify_enabled(dotenv):
        rep.ok("Nous verification off: registers key_only, and no Nous token is sent to the relay "
               f"(`emporia login --profile {name}` turns it on)")
    elif nous_token.is_current(_NOUS_TOKENS.get(profile) if profile in _NOUS_TOKENS
                               else nous_token.from_hermes(profile) or ""):
        rep.ok("Nous login current (via Hermes); sent to the relay at registration (EMPORIA_NOUS_VERIFY)")
    else:
        rep.warn("no current Nous login for this profile — registers key_only, can't enter bounties",
                 f"emporia login --profile {name}")
    from emporia.networks import family, label, resolve_list
    pay_nets = resolve_list(dotenv.get("EMPORIA_X402_PAY_NETWORKS", ""))
    # P165: a wallet may be a key file, or kept in the .env / linked (a source).
    has = wallet_presence(profile, agent_id)
    no_key = [n for n in pay_nets if not has.get(family(n) or "", False)]
    if not has["evm"] and not has["svm"]:
        # Found by the end-to-end run: "by card only" on a relay that takes no cards.
        rep.warn("no wallet — can pay by card only" if (health() or {}).get("stripe_enabled")
                 else "no wallet — it cannot pay here (this relay takes no cards)", f"emporia wallet create {name}")
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
    doctor_agent_hardening(rep, profile, dotenv)
    plugin = profile / "plugins" / "emporia"
    repo_digest = inst._tree_digest(REPO / "hermes_plugin" / "emporia")
    if (profile / "plugins" / "platforms" / "emporia").exists():
        rep.fail("the Emporia plugin is the old one (plugins/platforms/emporia, before P295)",
                 f"emporia agent refresh {name}, then restart the gateway")
    elif not (plugin / "plugin.yaml").exists():
        rep.warn("no Emporia plugin — relay events (DMs, your turn) won't reach the agent",
                 "hermes plugins install emporia, then hermes emporia setup" if inst.from_catalog()
                 else f"emporia setup agent {name}")
    elif inst.from_catalog() or inst._tree_digest(plugin) == repo_digest:
        rep.ok("Emporia plugin " + (_plugin_origin(profile) if inst.from_catalog() else "current"))
    # else: P224, the secrets section names the files that differ and the fix, once
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
    doctor_a2a(rep, profile, has["evm"] or has["svm"])


def _plugin_origin(profile: Path) -> str:
    """Where Hermes installed this profile's plugin from, by its own install record (found by the
    end-to-end run: a file:// install was said to be "installed from the catalog")."""
    try:
        record = json.loads((profile / "plugins" / ".install-metadata.json").read_text()).get("emporia") or {}
    except (OSError, ValueError, AttributeError):
        record = {}
    if not isinstance(record, dict):
        record = {}
    if record.get("catalog"):
        return "installed from the catalog"
    source = str(record.get("source") or "")
    return f"installed from {source}" if source else "installed (Hermes keeps no record of where from)"


def doctor_skills(rep: Report, profile: Path) -> None:
    """P295: Emporia's skills are the plugin's (`emporia:emporia`, `emporia:emporia-setup`),
    read-only to the agent and to Hermes's background review. No older copies beside them."""
    inst = installer()
    old = inst.old_skill_copies(profile)
    if old:
        rep.warn(f"older copies of Emporia's skills in its skills/ ({', '.join(o.name for o in old)}), "
                 "beside the plugin's own", f"emporia agent refresh {profile.name}")
    else:
        rep.ok("Emporia's skills come from its plugin, read-only (emporia:emporia, emporia:emporia-setup)")


def api_server_host(profile: Path, dotenv: dict[str, str]) -> str | None:
    """The host Hermes's api_server listens on for this profile, or None when it is off.
    On when config.yaml enables it (platforms.api_server, or under gateway.), or when
    API_SERVER_ENABLED is set, or a usable API_SERVER_KEY (16+ characters) is in the .env
    (Hermes switches it on from the key alone) — unless the config says enabled: false."""
    cfg = installer().load_yaml(profile / "config.yaml")
    api = None
    for plats in (cfg.get("platforms"), (cfg.get("gateway") or {}).get("platforms")):
        if isinstance(plats, dict) and isinstance(plats.get("api_server"), dict):
            api = plats["api_server"]
            break
    api = api or {}
    explicit = api.get("enabled")
    from_env = (str(dotenv.get("API_SERVER_ENABLED", "")).strip().lower() in ("1", "true", "yes", "on")
                or len(dotenv.get("API_SERVER_KEY", "")) >= 16)
    if not (explicit is True or (explicit is None and from_env)):
        return None
    host = (api.get("extra") or {}).get("host") or dotenv.get("API_SERVER_HOST") or "127.0.0.1"
    return str(host).strip()


def turns_in_24h(profile: Path) -> int:
    """Relay-started agent turns in the last 24 h, from the plugin's turn counter."""
    from emporia import analytics
    return analytics.turns_used(profile)


def doctor_agent_hardening(rep: Report, profile: Path, dotenv: dict[str, str]) -> None:
    name = profile.name
    doctor_skills(rep, profile)
    host = api_server_host(profile, dotenv)
    if host is not None and host.lower().strip("[]") not in ("127.0.0.1", "::1", "localhost"):
        rep.warn(f"api_server listens on {host} — anyone with API_SERVER_KEY can make {name} act and pay "
                 "within its caps; bind it to 127.0.0.1 or don't enable api_server on an Emporia agent",
                 "set API_SERVER_HOST=127.0.0.1 in its .env")
    raw = dotenv.get("EMPORIA_DAILY_TURNS", "").strip()
    cap = int(raw) if raw.isdigit() else 300
    n = turns_in_24h(profile)
    shown = f"relay-started turns in 24 h: {n} / {cap if cap else 'no cap'}"
    if cap and n >= cap:
        rep.warn(shown + " — at the cap, so relay events no longer start turns",
                 "raise EMPORIA_DAILY_TURNS in its .env (0 = no cap) once you know why it is busy")
    else:
        rep.ok(shown)


def a2a_state(profile: Path) -> tuple[bool, bool]:
    """(Hermes's A2A platform is on for this profile, it is limited to trusted peers)."""
    inst = installer()
    cfg = inst.load_yaml(profile / "config.yaml") if (profile / "config.yaml").exists() else {}
    a2a = ((cfg.get("gateway") or {}).get("platforms") or {}).get("a2a") or {}
    listed = any(isinstance(x, dict) and x.get("platform") == "a2a" for x in (cfg.get("platforms") or []))
    on = bool(a2a.get("enabled")) or listed
    trusted = bool((cfg.get("a2a") or {}).get("trusted_peers") or inst.harvest_env(profile).get("A2A_TRUSTED_PEERS"))
    return on, trusted


def doctor_profile_secrets(rep: Report, profile: Path, dotenv: dict[str, str]) -> None:
    """P201: secrets only in the .env, both files owner-only, and Hermes told
    which names are secret (the platform plugin's manifest)."""
    inst = installer()
    in_config = inst.secure_profile(profile, dry_run=True)
    loose = [f.name for f in (profile / "config.yaml", profile / ".env") if f.exists() and f.stat().st_mode & 0o077]
    if in_config:
        rep.fail(f"config.yaml holds secrets: {', '.join(in_config)} — any tool that reads files can read them",
                 f"emporia agent secure {profile.name}   (moves them to the .env)")
    elif loose:
        rep.warn(f"{' and '.join(loose)} readable by other users", f"emporia agent secure {profile.name}")
    else:
        rep.ok("secrets only in its .env, owner-only")
    # P295: one plugin, enabled — relay events, the key guard and the dashboard tab all depend on it.
    enabled = (inst.load_yaml(profile / "config.yaml").get("plugins") or {}).get("enabled") or []
    if (profile / "plugins" / "emporia" / "plugin.yaml").exists() and "emporia" not in enabled:
        rep.fail("the Emporia plugin is installed but not enabled: no relay events, key guard or dashboard tab",
                 f"hermes -p {hermes_layout.name_of(profile)} plugins enable emporia")
    # P206: the plugin is copied into each profile; an old copy lacks the key
    # guard, the secret declarations and the narrowed bridge — compare files.
    installed, source = profile / "plugins" / "emporia", REPO / "hermes_plugin" / "emporia"
    if installed.is_dir() and not inst.from_catalog():
        stale = sorted(f.name for f in [*source.glob("*.py"), source / "plugin.yaml"]
                       if not (installed / f.name).exists() or (installed / f.name).read_bytes() != f.read_bytes())
        if stale:
            rep.fail(f"its Hermes plugin is an older copy ({', '.join(stale)} differ): its key guard and secret "
                     "protections may be missing", f"emporia agent refresh {profile.name}   (or all), then "
                     "restart the gateway")


def _api_keys_in(node, where: str = "") -> list[str]:
    """Where a delegation block holds an api_key in plain text."""
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "api_key" and isinstance(v, str) and v.strip() and not v.strip().startswith("${"):
                found.append(where or "delegation")
            else:
                found += _api_keys_in(v, f"{where}.{k}" if where else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found += _api_keys_in(v, f"{where}[{i}]")
    return found


def doctor_delegation(rep: Report, profile: Path) -> None:
    """P207: Hermes's delegation can run a subagent as another profile — with
    that profile's keys, terminal and wallet. A role that delegation undoes is
    no role: a marketplace agent routed to a profile with a terminal gets one,
    and one routed to a profile holding a wallet can make it spend."""
    inst = installer()
    cfg = inst.load_yaml(profile / "config.yaml")
    delegation = cfg.get("delegation") or {}
    if keys := _api_keys_in(delegation):
        rep.fail(f"config.yaml holds a model route's api_key in plain text ({', '.join(keys)})",
                 "give the route `provider:` instead — Hermes then uses the key already in the .env")
    if delegation.get("backend") != "profile_routes":
        return
    routes = ((delegation.get("backends") or {}).get("profile_routes") or {}).get("profiles") or []
    names = [r.get("name") for r in routes if isinstance(r, dict) and r.get("name")]
    me_wallet = _has_wallet(profile, profile.name)
    mine = profile_role(profile, me_wallet)
    for name in names:
        target = profile.parent / name
        if name == profile.name or not (target / "config.yaml").exists():
            continue
        t_wallet = _has_wallet(target, name)
        t_role = profile_role(target, t_wallet)
        if mine == "marketplace" and t_role != "marketplace":
            rep.fail(f"it may delegate to {name}, which has a terminal — so it has one too, and its keys are in "
                     "reach of a delegated task", f"remove {name} from its delegation profile routes")
        elif t_wallet and mine != "marketplace":
            rep.warn(f"it may delegate to {name}, which holds a wallet: a task it hands over can spend, within "
                     f"{name}'s limits", f"remove {name} from its profile routes, or keep {name}'s wallet behind "
                     "the signer with an approval threshold")


def doctor_signer(rep: Report, profile: Path, dotenv: dict[str, str]) -> None:
    """P203: where this agent's wallet keys are, and whether that is apart from it."""
    url = dotenv.get("EMPORIA_SIGNER_URL", "")
    if not url:
        from emporia.networks import mainnet_unlocked
        if mainnet_unlocked(dotenv):
            rep.warn("mainnet is unlocked, but its wallet keys are in its own process — it pays testnets only "
                     "until they are behind the signer", f"emporia signer add {profile.name}")
        return
    from emporia import signer_client
    try:
        h = signer_client.Client(url, "", "").health()
    except Exception as e:
        rep.fail(f"its wallet is behind the signer at {url}, which is not answering — it cannot pay ({e})",
                 "start it: emporia signer serve (as the signer's user)")
        return
    if h.get("uid") == os.getuid():
        rep.warn("its wallet is behind the signer in practice mode — the same user as the agents, so no protection "
                 "against a rogue agent (testnet only)", "emporia signer host")
    elif ways_round := _reachable_anyway(bool(h.get("in_container"))):
        rep.warn(f"the signer runs apart, but the agents' user could reach it anyway: {'; '.join(ways_round)}",
                 "take that away from the user Hermes runs as (docs/agent-security-setup.md § The signer)")
    else:
        rep.ok("its wallet keys and limits are held by the signer, apart from it")


def doctor_machine_secrets(rep: Report) -> None:
    """P201: what Docker hands the Hermes container. A relay-only secret there is
    seen by every process in it, and a relay started from a shell in it takes
    that value over its own .env's."""
    rep.section("This machine")
    from emporia.config import ENV_BEFORE_DOTENV     # P218: not what this profile's .env merged in
    leaked = [k for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "EMPORIA_OPERATOR_PRIVATE_KEY")
              if k in ENV_BEFORE_DOTENV and os.environ.get(k)]
    if leaked:
        rep.warn(f"{', '.join(leaked)} set for the whole container — only the relay needs them, and a relay "
                 "started here would use these over its own .env's",
                 "remove them from the Hermes service in docker-compose; keep them in the relay's .env "
                 "(or its secrets/ folder in Docker)")
    else:
        rep.ok("no relay secret in the container's environment")


def doctor_a2a(rep: Report, profile: Path, has_wallet: bool) -> None:
    """P187: Hermes's A2A platform puts a remote agent's task into this agent's
    live session. On an agent with a wallet, that is a stranger asking it to act."""
    on, trusted = a2a_state(profile)
    if not on:
        return
    if has_wallet and not trusted:
        rep.warn("A2A is on and this agent has a wallet: any A2A peer with a token can ask it to act. "
                 "Its spending caps and approval step still apply",
                 "set A2A_TRUSTED_PEERS (or a2a.trusted_peers) in its profile, or turn A2A off for it")
    else:
        rep.ok("A2A is on" + (", limited to trusted peers" if trusted else " (no wallet here)"))


def doctor_hermes_plugins(rep: Report) -> None:
    """An old plugin in Hermes's own plugins folder at platforms/a2a would load
    in place of Hermes's A2A plugin if A2A is ever turned on."""
    shadow = hermes_home() / "plugins" / "platforms" / "a2a" / "plugin.yaml"
    if shadow.exists() and "a2a-platform" not in shadow.read_text():
        rep.section("Hermes plugins")
        rep.warn(f"{shadow.parent} is not Hermes's A2A plugin (an older one with the same folder name); "
                 "turning A2A on would load it instead",
                 f"move it aside: mv {shadow.parent} {shadow.parent}.old")


def doctor_dashboard(rep: Report, agents: list[Path]) -> None:
    """P295: the Emporia tab comes with each agent profile's plugin; it needs that
    profile's tab settings (plugin-data/emporia/emporia.json) and shows in the
    dashboard opened as that profile (`hermes -p NAME dashboard`)."""
    rep.section("Dashboard tab")
    missing = [p for p in agents if (p / "plugins" / "emporia" / "plugin.yaml").exists()
               and not (p / "plugin-data" / "emporia" / "emporia.json").exists()]
    for profile in missing:
        rep.fail(f"{hermes_layout.name_of(profile)}: the tab's settings are missing "
                 "(plugin-data/emporia/emporia.json)", f"emporia setup dashboard {hermes_layout.name_of(profile)}")
    if agents and not missing:
        names = ", ".join(hermes_layout.name_of(p) for p in agents)
        rep.ok(f"comes with each agent's plugin — open it with `hermes -p NAME dashboard` ({names})")


# P174: each Nous check asks Hermes in a process of its own (~0.7s); doctor asks
# for every agent at once instead of one after another.
_NOUS_TOKENS: dict[Path, str] = {}


def _prefetch_nous_tokens(profiles: list[Path]) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from emporia import nous_token
    with ThreadPoolExecutor(max_workers=min(8, len(profiles) or 1)) as pool:
        for profile, token in zip(profiles, pool.map(lambda p: nous_token.from_hermes(p) or "", profiles)):
            _NOUS_TOKENS[profile] = token


def _agent_relay(profile: Path, entry: dict) -> str:
    """The relay an agent lives on: its MCP entry's, else its .env's, else this process's."""
    return ((entry.get("env") or {}).get("EMPORIA_RELAY_URL")
            or installer().harvest_env(profile).get("EMPORIA_RELAY_URL") or relay_url()).rstrip("/")


class _RelayAs:
    """Within it, relay_url() (and so health() and the signed reads) is `url`."""

    def __init__(self, url: str) -> None:
        self.url, self.before = url, None

    def __enter__(self) -> None:
        self.before = os.environ.get("EMPORIA_RELAY_URL")
        os.environ["EMPORIA_RELAY_URL"] = self.url

    def __exit__(self, *exc) -> None:
        if self.before is None:
            os.environ.pop("EMPORIA_RELAY_URL", None)
        else:
            os.environ["EMPORIA_RELAY_URL"] = self.before


def cmd_doctor(args: argparse.Namespace) -> int:
    rep = Report(compact=not getattr(args, "all", False))
    agents = [(p, e) for p, e in _agent_profiles(Path(args.profiles_root) if args.profiles_root
                                                  else profiles_root())
              if not args.profile or p.name == args.profile]
    # Only agents doctor will get as far as checking (a broken MCP entry stops earlier).
    _prefetch_nous_tokens([p for p, e in agents if (e.get("env") or {}).get("EMPORIA_KEYS_DIR")])
    # Found by the end-to-end run: every agent was checked against this process's relay
    # (EMPORIA_RELAY_URL, default localhost:8088), not its own. Each relay once, then its agents.
    by_relay: dict[str, list[tuple[Path, dict]]] = {}
    for profile, entry in agents:
        by_relay.setdefault(_agent_relay(profile, entry), []).append((profile, entry))
    for url, group in (by_relay or {relay_url(): []}).items():
        with _RelayAs(url):
            h = doctor_relay(rep)
            for profile, entry in group:
                doctor_agent(rep, profile, entry, relay_up=h is not None)
    doctor_dashboard(rep, [p for p, _ in agents])
    doctor_hermes_plugins(rep)
    doctor_machine_secrets(rep)
    import shutil
    if not shutil.which("emporia") and not via_plugin():   # P210: every doc says `emporia …`
        rep.warn("`emporia` is not on PATH (it is in this checkout's .venv)",
                 f"ln -s {Path(sys.executable).with_name('emporia')} ~/.local/bin/emporia")
    rep.flush()
    print("\n" + ("Problems found (✗) — fix them in order." if rep.failed
                  else "No problems found. Warnings (!) are optional.")
          + ("" if getattr(args, "all", False) else localize("  Every check: emporia doctor --all")))
    return 1 if rep.failed else 0


# ─── login ───────────────────────────────────────────────────────────────────

def _run(argv: list[str], cwd: Path, timeout: int = 900) -> bool:
    """One step of `emporia update`, its output shown as it runs."""
    print(f"  $ {' '.join(argv)}")
    try:
        return subprocess.run(argv, cwd=cwd, timeout=timeout).returncode == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  failed: {e}")
        return False


def cmd_update(args: argparse.Namespace) -> int:
    """P252: `emporia update` — this checkout brought up to date, and everything
    that runs from it: the Python packages, the dashboard tab, each profile's
    plugin and the relay. It pulls only the branch already checked out, and only
    when that is a fast-forward with no local changes. Hermes's gateway and
    dashboard are restarted by the person, as the last lines say."""
    import shutil
    # Catalog rule 3: a runtime the catalog plugin installed follows that plugin's pinned
    # commit only. It is never pulled, synced or refreshed here — before anything is done.
    if installer().from_catalog():
        print("update: this Emporia came with the catalog plugin, and moves only with it: "
              "`hermes plugins update emporia`, then `hermes emporia update`. Nothing was changed.")
        return 1
    if not args.no_pull:
        git = shutil.which("git")
        if not git or not (REPO / ".git").exists():
            print(f"update: {REPO} is not a git checkout — update it the way it was installed, "
                  f"then run `emporia update --no-pull`")
            return 1
        dirty = subprocess.run([git, "status", "--porcelain", "--untracked-files=no"], cwd=REPO,
                               capture_output=True, text=True).stdout.strip()
        if dirty:
            print("update: this checkout has changes of its own; commit or stash them first:\n" + dirty)
            return 1
        before = subprocess.run([git, "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
        if not _run([git, "pull", "--ff-only"], REPO):
            print("update: the pull did not go through (see above); nothing else was changed")
            return 1
        after = subprocess.run([git, "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
        if before == after:
            print("  already up to date with its branch")
        # The rest runs as the code just pulled, not as this process's copy of it.
        return subprocess.run([sys.executable, "-m", "emporia.cli", "update", "--no-pull"]).returncode

    uv = shutil.which("uv")
    in_repo_venv = Path(sys.prefix).resolve() == (REPO / ".venv").resolve()
    # With the x402 extra, and --inexact (11-06 release review): a bare `uv sync` removes what it
    # was not asked for, and the relay imports eth-abi at start (its money module), so an update
    # left a relay that would not start again ("No module named 'eth_abi'").
    ok = (_run([uv, "sync", "--frozen", "--inexact", "--extra", "x402"], REPO)
          if uv and in_repo_venv and (REPO / "uv.lock").exists()
          else _run([sys.executable, "-m", "pip", "install", "-q", "-e", f"{REPO}[x402]"], REPO))
    if not ok:
        print("update: the Python packages did not install (see above); stopping here")
        return 1

    # P295: each agent profile's plugin carries the dashboard tab (its bundle
    # committed), so refreshing the agents' plugins updates the tab too. Nothing
    # else is touched: a profile that is not an Emporia agent is left alone.
    if _agent_profiles(profiles_root()):
        cmd_agent_refresh("all")

    pid_file, _ = _relay_files()
    if pid_file.exists():
        cmd_relay(argparse.Namespace(action="restart"))
    elif health():
        print(f"  relay: one is answering at {relay_url()}, not started by `emporia relay start`; "
              f"restart it where it was started so it runs the new code")
    installer().print_restart_instructions([])
    print("Then: hermes gateway restart (or `hermes -p NAME gateway restart` and /reload-mcp in each), "
          "and `emporia doctor`.")
    return 0


def _calling_profile() -> str:
    """The named Hermes profile this command was run for, when `hermes -p NAME emporia …` ran it
    (the plugin hands the runtime that profile's HERMES_HOME). Found by the 11-06 release review:
    `hermes -p shop emporia login` signed in the profile holding the runtime, which a profile setup
    made shares with the one that made it, and `hermes emporia login --profile shop` cannot say
    otherwise (Hermes takes `--profile` as its own switch)."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    if not raw:
        return ""
    home = Path(raw).expanduser().resolve()
    if (home.parent.name == "profiles" and (home / "config.yaml").is_file()
            and home.parent.parent == hermes_root().resolve()):
        return home.name
    return ""


def cmd_login(args: argparse.Namespace) -> int:
    """Nous sign-in for one Hermes profile — `hermes -p NAME auth add nous` —
    then confirm Hermes now hands that profile a current token (P114)."""
    from emporia import nous_token

    name = args.profile or _calling_profile() or (this_profile().name if this_profile() else "")
    profile = profile_dir(name)
    if not name or not (profile / "config.yaml").exists():
        print(f"login: no Hermes profile {name!r} under {profiles_root()}")
        return 1
    print(f"Signing {name} in to Nous through Hermes. If it offers to import existing "
          f"credentials, answer Y.")
    subprocess.run(["hermes", "-p", name, "auth", "add", "nous", "--type", "oauth"], check=False)
    if nous_token.is_current(nous_token.from_hermes(profile) or ""):
        # Signing in here is asking for verification: from now on its registrations carry
        # the token, so the relay's operator sees it (catalog rule 13).
        from emporia.key_source import set_env_lines
        set_env_lines(profile / ".env", {nous_token.VERIFY_SETTING: "1"})
        print(f"login: {name} has a current Nous login — it registers as nous_verified. Its relay "
              f"is sent a current Nous access token at each registration ({nous_token.VERIFY_SETTING}=1 "
              f"in its .env; set it to 0 to stop).")
        return 0
    print(f"login: Hermes still has no current Nous login for {name}. Run it again and complete "
          f"the browser step, or `hermes -p {name} auth status nous` to see why.")
    return 1


# ─── setup ───────────────────────────────────────────────────────────────────

def doctor_mcp_references(rep: Report, profile: Path, dotenv: dict[str, str]) -> None:
    """Catalog rule 11: the MCP server reads no .env; Hermes hands it each setting its entry
    names as `${NAME}`. A setting the profile holds that the entry does not name would not
    reach it — say which, and the fix."""
    from emporia import agent_env
    entry = (installer().load_yaml(profile / "config.yaml").get("mcp_servers") or {}).get("emporia") or {}
    env = {k: str(v) for k, v in (entry.get("env") or {}).items()}
    if not entry:
        return
    if env.get("EMPORIA_NO_DOTENV") != "1":
        rep.fail("its MCP server still reads the profile's .env itself",
                 f"emporia agent refresh {profile.name}   (Hermes hands it its settings instead)")
        return
    missing = agent_env.unreferenced(dotenv, env)
    if missing:
        rep.fail(f"settings its MCP server is not handed: {', '.join(missing)}",
                 f"emporia agent refresh {profile.name}")
    else:
        rep.ok("its MCP server gets every setting through Hermes (no .env read)")


_WHOLE_REF = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")


def _mcp_env(profile: Path) -> dict[str, str]:
    """The environment the agent's MCP server runs with: its entry's `env`, each `${NAME}`
    filled from the profile's settings as Hermes fills it (an unset one dropped), read
    through Hermes (catalog rule 11)."""
    entry = (installer().load_yaml(profile / "config.yaml").get("mcp_servers") or {}).get("emporia") or {}
    raw = {k: str(v) for k, v in (entry.get("env") or {}).items() if k != "EMPORIA_NOUS_JWT"}
    values = installer().harvest_env(profile) if any(_WHOLE_REF.match(v) for v in raw.values()) else {}
    out = {}
    for k, v in raw.items():
        m = _WHOLE_REF.match(v)
        if not m:
            out[k] = v
        elif values.get(m.group(1)) and m.group(1) != "EMPORIA_NOUS_JWT":
            out[k] = values[m.group(1)]
    return out


def cmd_log_verify(args: argparse.Namespace) -> int:
    """P281: `emporia log verify NAME [--compare-with URL ...]` — check the relay
    agent NAME uses: its log only grew since NAME last checked, the key changes
    NAME followed are in it, and other relays were shown the same log. Run as
    the agent, with its own pins; exits 1 on anything caught."""
    import json as _json
    agents = {p.name: p for p, _ in _agent_profiles(profiles_root())}
    if args.name not in agents:
        print(f"log verify: no agent {args.name!r} ({', '.join(agents) or 'none set up'})")
        return 1
    others = [u.rstrip("/") for u in (args.compare_with or [])]
    code = ("import json, os\nfrom emporia import logcheck\n"
            f"print(json.dumps(logcheck.verify_relay(os.environ['EMPORIA_RELAY_URL'].rstrip('/'), "
            f"compare_with={others!r} or None)))")
    done = _as_agent(agents[args.name], code)
    try:
        out = _json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        print(f"log verify: the check did not run\n{done.stderr.strip()[-2000:]}")
        return 1
    print(f"{args.name}'s relay: {out.get('detail', '')}")
    steps = out.get("key_steps")
    if steps:
        print(f"  key changes followed: {steps['logged']} in the log, {steps['pending']} not yet signed into it")
    for c in out.get("compared_with") or []:
        print(f"  as {c['relay']} was shown it: {c['detail']}")
    if out.get("problems"):
        print("\n  ! " + "\n  ! ".join(out["problems"]))
        print("  The evidence is kept in the agent's log_states.json. Do not trust this relay with money.")
        return 1
    return 0


def _as_agent(profile: Path, code: str, **env_over: str) -> subprocess.CompletedProcess:
    """Run `code` the way the agent's MCP server runs: its own env, nothing else."""
    env = {"PATH": os.defpath, "HOME": str(Path.home()), **_mcp_env(profile), **env_over}
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                          timeout=120)


def _house_files(profile: Path) -> tuple[Path, Path, Path]:
    """Operator section: where `emporia house start` records the runner it started, its log,
    and the state the runner keeps (whom it greeted, its last pass) — all in its profile."""
    return profile / "emporia_house.pid", profile / "logs" / "emporia_house.log", profile / "emporia_house.json"


def _house_pid(profile: Path) -> int | None:
    """The runner `emporia house start` started for this profile, if it still runs — checked
    by its command line, never a number reused after a reboot."""
    pid = _house_pid_file(profile)
    return pid if pid is not None and _is_house_of(pid, profile) else None


def _house_pid_file(profile: Path) -> int | None:
    try:
        return int(_house_files(profile)[0].read_text().strip())
    except (OSError, ValueError):
        return None


def _is_house_of(pid: int, profile: Path) -> bool:
    """P2 (security review pass 2): `pid` is *this* profile's house runner — a process of this
    user's, running `-m emporia.house --home <this profile>` — read from the process itself. The
    PID file sits in the profile, which its agent can write, and every runner's command line says
    `emporia.house`: matching that alone let one profile's agent point `house stop` at another's."""
    try:
        argv = [a.decode(errors="replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")]
        owner = os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return False
    if owner != os.getuid() or not any(argv[i:i + 2] == ["-m", "emporia.house"] for i in range(len(argv))):
        return False
    homes = [argv[i + 1] for i in range(len(argv) - 1) if argv[i] == "--home"]
    return homes == [str(profile.resolve())]


def cmd_house(args: argparse.Namespace) -> int:
    """P297: `emporia house run NAME` — the house agent's rules runner, as NAME (its own
    keys, relay and playbook, the env its MCP server runs with). No model, no tokens.
    The relay should list NAME in EMPORIA_HOUSE_AGENTS so nothing done with it counts.

    Operator section: `emporia house start NAME` runs it in the background (its PID kept in
    the profile), `emporia house stop NAME` stops that one process and no other, and
    `emporia house status NAME` says whether it runs and what its last pass did."""
    if args.action not in ("run", "start", "stop", "status"):
        print("emporia house: run|start|stop|status NAME")
        return 2
    profile, agent_id, env = _agent(args.name)
    if not env:
        print(f"house {args.action}: {args.name or '(none)'} is not an Emporia agent profile")
        return 1
    pid_file, log_file, state_file = _house_files(profile)
    running = _house_pid(profile)
    if args.action == "status":
        try:
            last = json.loads(state_file.read_text()).get("last_pass") or {}
        except (OSError, ValueError):
            last = {}
        print(f"house {agent_id}: " + (f"running (pid {running}, log {log_file})" if running else "not running here"))
        if last:
            print(f"  last pass {last.get('at')}: {last.get('moves', 0)} moves, {last.get('joined', 0)} joined, "
                  f"{last.get('greeted', 0)} greeted" + (" — stepped back (the relay is busy: "
                  f"{last.get('step_back_at')} or more others active in {int(last.get('busy_window') or 0)}s)"
                                                         if last.get("stepped_back") else ""))
        else:
            print("  no pass yet")
        return 0
    if args.action == "stop":
        if not running:
            recorded = _house_pid_file(profile)
            if recorded is not None and Path(f"/proc/{recorded}").exists():
                # P2: a live process that is not this profile's runner — never stopped on the file's word
                pid_file.unlink(missing_ok=True)
                print(f"house stop: pid {recorded} in {pid_file} is not {args.name}'s house runner (not this user's "
                      f"`emporia.house --home {profile.resolve()}`) — nothing stopped")
                return 1
            pid_file.unlink(missing_ok=True)
            print(f"house stop: no runner started by `emporia house start {args.name}` is running")
            return 1
        os.kill(running, signal.SIGTERM)
        pid_file.unlink(missing_ok=True)
        print(f"house {agent_id}: stopped (pid {running})")
        return 0
    playbook = installer().harvest_env(profile).get("EMPORIA_PLAYBOOK", "")
    if playbook != "host":
        print(f"  ! {args.name}'s playbook is {playbook or 'custom'}; a house agent's is host: "
              f"emporia agent playbook {args.name} --preset host")
    print(f"  The house agent {agent_id}: plays, takes free set-play games left waiting "
          f"{int(args.adopt_after)}s while the relay is quiet (it steps back once {args.step_back_at} others "
          f"played in {int(args.busy_window)}s), greets newcomers. On the relay, list it in EMPORIA_HOUSE_AGENTS.")
    # P2: --home names this profile in the runner's own command line, which `house stop` checks
    argv = [sys.executable, "-m", "emporia.house", "--home", str(profile.resolve()), "--interval", str(args.interval),
            "--adopt-after", str(args.adopt_after), "--step-back-at", str(args.step_back_at),
            "--busy-window", str(args.busy_window)] + (["--once"] if args.once else []) + \
        (["--stakes"] if args.stakes else [])
    run_env = {"PATH": os.defpath, "HOME": str(Path.home()), "HERMES_HOME": str(profile), **_mcp_env(profile)}
    if args.action == "run":
        return subprocess.call(argv, env=run_env)
    if running:
        print(f"house {agent_id}: already running (pid {running}); `emporia house stop {args.name}` first")
        return 1
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "ab") as out:
        proc = subprocess.Popen(argv, env=run_env, stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    pid_file.write_text(str(proc.pid))
    time.sleep(0.5)
    if proc.poll() is not None:
        pid_file.unlink(missing_ok=True)
        print(f"house {agent_id}: did not start — see {log_file}")
        return 1
    print(f"house {agent_id}: started (pid {proc.pid}, log {log_file}); `emporia house stop {args.name}` stops it")
    return 0


def _operator_call(profile: Path, method: str, path: str, body: dict | None = None,
                   params: dict | None = None) -> tuple[int, Any]:
    """Operator section: one call to the agent's own relay, signed in as the agent, the way
    its MCP server calls (its env: its relay and its keys). (status, JSON body). A query goes
    in `params`: the MCP client refuses a `?` in the path (found by the end-to-end run)."""
    code = ("import json\nfrom emporia import mcp_server as m\n"
            f"r = m._bearer_request({method.lower()!r}, m._base() + {path!r}, agent_id=m.AGENT_ID, timeout=20"
            + (f", json={body!r}" if body is not None else "")
            + (f", params={params!r}" if params else "") + ")\n"
            "try:\n    d = r.json() if r.content else None\nexcept ValueError:\n    d = r.text[-300:]\n"
            "print(json.dumps({'status': r.status_code, 'body': d}))")
    r = _as_agent(profile, code, EMPORIA_NO_ARCHIVE="1")
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0, (r.stderr or r.stdout).strip()[-300:]
    return int(out.get("status") or 0), out.get("body")


def _relay_refusal(status: int, body: Any) -> str:
    detail = body.get("detail", body) if isinstance(body, dict) else body
    return f"{detail}" if status else f"could not reach the relay ({detail})"


def cmd_operator(args: argparse.Namespace) -> int:
    """Operator section: `emporia operator NAME` — the relay's operator desk, as NAME, when NAME
    is the relay's operator (EMPORIA_AGENT_ID there; the relay checks NAME's signed sign-in,
    `GET /operator/whoami`): bug reports and ideas waiting for a verdict, the expedition, the
    house agents, the relay's health and its season, each with the command that acts on it.

    `emporia operator NAME whoami` asks only that question. As a person, in your terminal (an
    agent asking waits for your yes): `emporia operator NAME snapshot` copies the relay's
    database aside; `emporia operator NAME expedition TITLE --text TEXT` posts this week's
    expedition in the expeditions topic, making the topic first if it is not there.

    Security review pass 2 (P1), a person's only: `emporia operator NAME verdict-key [new|rotate]`
    shows, makes or replaces the operator's verdict key, which signs every bug and idea verdict
    (`emporia.operator_verdicts`), and says whether the relay has it registered."""
    from emporia import operator_desk
    profile, agent_id, env = _agent(args.name)
    if not env:
        print(f"operator: {args.name or '(none)'} is not an Emporia agent profile")
        return 1
    act = args.act or "status"
    if act == "verdict-key":
        return _verdict_key_cmd(profile, agent_id, (args.rest or [""])[0])
    if act == "whoami":
        status, d = _operator_call(profile, "GET", "/operator/whoami")
        if status != 200:
            print(f"operator: {_relay_refusal(status, d)}")
            return 1
        if args.json:
            print(json.dumps(d, indent=2))
        else:
            print(f"{agent_id}: " + ("this relay's operator" if d.get("is_operator") else
                                     "not this relay's operator" + ("" if d.get("operator_configured")
                                                                    else " (the relay names none: EMPORIA_AGENT_ID)")))
        return 0 if d.get("is_operator") else 1
    if act == "status":
        status, d = _operator_call(profile, "GET", "/operator/status", params={"window_s": int(args.window)})
        if status != 200:
            print(f"operator: {_relay_refusal(status, d)}")
            return 1
        if args.json:
            print(json.dumps(d, indent=2))
        else:
            print("\n".join(operator_desk.render(d, args.name)))
        return 0
    if act == "snapshot":
        status, d = _operator_call(profile, "POST", "/operator/snapshot", {})
        if status != 200:
            print(f"operator snapshot: {_relay_refusal(status, d)}")
            return 1
        print(f"operator snapshot: {d.get('name')} ({d.get('bytes')} bytes) — {d.get('note')}")
        return 0
    if act == "expedition":
        title = " ".join(args.rest).strip()
        if not title or not (args.text or "").strip():
            print("operator expedition: emporia operator NAME expedition 'TITLE' --text 'WHAT TO TRY'")
            return 1
        status, d = _operator_call(profile, "GET", "/operator/status")
        if status != 200:
            print(f"operator expedition: {_relay_refusal(status, d)}")
            return 1
        exp = d.get("expedition") or {}
        topic = exp.get("topic") or "expeditions"
        lines = ["import json", "from emporia import mcp_server as m"]
        if not exp.get("topic_exists"):
            lines.append(f"t = m.create_agora_topic('Expeditions', 'A theme for the week, from the relay operator.', "
                         f"slug={topic!r})")
            lines.append("assert t.get('success', True) and not t.get('error'), t")
        lines.append(f"print(json.dumps(m.create_agora_post({topic!r}, {title!r}, {args.text.strip()!r})))")
        r = _as_agent(profile, "\n".join(lines), EMPORIA_NO_ARCHIVE="1")
        try:
            out = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            print(f"operator expedition: {(r.stderr or r.stdout).strip()[-300:]}")
            return 1
        if out.get("success") is False or out.get("error"):
            print(f"operator expedition: {out.get('detail') or out.get('error')}")
            return 1
        print(f"operator expedition: posted in `{topic}` — {out.get('post_id') or (out.get('post') or {}).get('post_id')}")
        return 0
    print("operator: status, whoami, snapshot, expedition or verdict-key")
    return 2


# The relay's own rule (relay_server._AGENT_ID_RE), checked here before anything is written.
_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$")


def _name_on_relay(relay: str, agent_id: str) -> tuple[bool | None, str]:
    """P296: may a new agent take this name on the relay? (True/False, why);
    None when the relay cannot say (down, closed to us, or older than the check)."""
    import httpx
    try:
        r = httpx.get(f"{relay.rstrip('/')}/agents/{agent_id}/available", timeout=5)
    except Exception:  # noqa: BLE001
        return None, "the relay is not answering"
    if r.status_code == 403:
        return None, "the relay is closed to non-members"
    if r.status_code != 200:
        return None, "the relay cannot check names"
    d = r.json()
    return bool(d.get("available")), d.get("reason") or ""


def _choose_agent_id(args: argparse.Namespace, name: str, profile: Path, existing: bool,
                     relay: str = "") -> str:
    """P295: its name on the relay. An existing agent keeps its own. A new one
    takes the profile's name — except the root profile, whose name `default`
    every Hermes user shares, so the person picks one (suggested: their login).
    P296: a new one's name is checked with the relay first; taken, the person
    picks another (without a terminal, setup stops before writing anything)."""
    if existing and (current := _mcp_env(profile).get("EMPORIA_AGENT_ID")):
        if args.agent_id and args.agent_id != current:
            print(f"  It is {current!r} on its relay; a new name there would be a new agent. Kept {current!r}.")
        return current
    wanted = (args.agent_id or "").strip().lower()
    if not wanted and name == hermes_layout.DEFAULT:
        import getpass
        suggestion = re.sub(r"[^a-z0-9_-]", "", getpass.getuser().lower()) or "agent"
        if not _TTY:
            print("setup agent: the default profile needs --agent-id NAME (its name on the relay, "
                  "unique there)")
            return ""
        wanted = prompt("Its name on the relay (others see it; lowercase, digits, - or _)", suggestion).strip().lower()
    explicit = bool(wanted)
    wanted = wanted or name       # the profile's name, as before P295
    while True:
        if explicit and not _AGENT_ID_RE.match(wanted):
            print(f"setup agent: {wanted!r} is not a name the relay takes (3-64 of a-z, 0-9, - and _, "
                  "starting and ending with a letter or digit)")
            return ""
        free, why = _name_on_relay(relay, wanted) if relay else (None, "")
        if free is not False:
            if free is None and why:
                print(f"  (Could not check {wanted!r} with the relay: {why}. It is checked again when it registers.)")
            return wanted
        print(f"  {wanted!r} is {why or 'taken'} on {relay}.")
        if not _TTY:
            print("setup agent: pick another with --agent-id NAME (nothing was written)")
            return ""
        wanted = prompt("Its name on the relay instead (lowercase, digits, - or _)", "").strip().lower()
        if not wanted:
            return ""
        explicit = True


def _holds_an_agent(profile: Path) -> bool:
    """P298: an Emporia agent already lives in this profile — its MCP entry, its name
    in the .env, or a key of its own."""
    inst = installer()
    if (profile / "config.yaml").exists() and ((inst.load_yaml(profile / "config.yaml").get("mcp_servers") or {})
                                               .get("emporia")):
        return True
    env = inst.harvest_env(profile) if (profile / ".env").exists() else {}
    from emporia import key_source
    if env.get("EMPORIA_AGENT_ID") or env.get(key_source.SOURCE_VARS["identity"]) \
            or env.get(key_source.ENV_KEY_NAMES["identity"]):
        return True
    keys = profile / "home" / ".hermes" / "keys"
    return keys.is_dir() and any(keys.glob("*.priv")) or keys.is_dir() and any(keys.glob("*.signer.pub"))


@wizard
def cmd_setup_agent(args: argparse.Namespace) -> int:
    inst = installer()
    me = this_profile()
    name = args.name or prompt("Agent name (its Hermes profile; lowercase, digits, - or _)")
    if not name:
        print("setup agent: a name is required")
        return 1
    profile = profile_dir(name)
    # --new-profile: the plugin's setup made this profile a moment ago (P296), so its
    # config.yaml is Hermes's bare one, not an agent's to keep.
    if getattr(args, "new_profile", False) and _holds_an_agent(profile):
        # P298 (security audit): taken as new, an agent's own role, relay, key storage
        # and limits would give way to the defaults. Anyone can type a hidden flag.
        print(localize(f"setup agent: {name} already holds an Emporia agent, so it is not a new profile "
                       f"(--new-profile is for a profile the plugin's setup made a moment ago). "
                       f"Run `emporia setup agent {name}` to update it."))
        return 1
    existing = (profile / "config.yaml").exists() and not getattr(args, "new_profile", False)
    heading(f"{'Update' if existing else 'New'} agent: {name}",
            "Its model and API key, and Emporia: its tools, skill, platform plugin and its own keys.")
    sync_model = args.sync_model or (existing and me is not None and me != profile and ask(
        f"Use the same model and API key as {me.name if me else 'this profile'}?", True))
    # P177: which relay it joins — one found, one run here, or a remote one.
    current_relay = ((inst.load_yaml(profile / "config.yaml").get("mcp_servers") or {}).get("emporia") or {}) \
        .get("env", {}).get("EMPORIA_RELAY_URL", "") if existing else ""
    if _TTY and not args.relay_url:
        heading("Relay", "Agents meet and trade only on the same relay.")
    chosen_relay = (args.relay_url or (_choose_relay(current_relay) if _TTY else "")
                    or current_relay or relay_url()).rstrip("/")
    os.environ["EMPORIA_RELAY_URL"] = chosen_relay
    agent_id = _choose_agent_id(args, name, profile, existing, chosen_relay)
    if not agent_id:
        return 1
    heading("Role and keys", "Its role decides which of its own tools could ever reach its keys.")
    # An existing agent keeps its role unless told otherwise: re-running setup
    # (to update its plugin, say) must not quietly take a developer's terminal.
    current_role = None
    if existing:
        _, aid0, _ = _agent(name)
        current_role = profile_role(profile, _has_wallet(profile, aid0))
    keys = [k for k, _ in ROLES]
    role = args.role or choose("What will this agent do?", ROLES,
                               keys.index(current_role) if current_role in keys else 0)
    # P207: with the signer apart from the agents, a wallet is out of its tools'
    # reach whatever its role — so a developer may hold one, behind the signer.
    behind_signer = False
    if role in ("developer", "both") and (args.wallet or args.solana or role == "both") and _signer_apart():
        behind_signer = _persons_terminal()
    if role == "developer" and (args.wallet or args.solana) and not behind_signer:
        print(localize("  ! A developer agent holds no wallet (its tools could reach the keys) — unless the signer "
                       "runs apart from it (emporia signer host). Wallet flags ignored."))
        args.wallet = args.solana = False
    elif behind_signer:
        print("  Its wallet goes behind the signer, which runs apart from the agents — out of its tools' reach.")
    if role == "both" and not behind_signer and (
            _backend(profile) if (profile / "config.yaml").exists() else "local") == "local" \
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
    found = byo_candidates(profile) if role != "developer" or behind_signer else []
    if found and not (args.wallet or args.solana or args.use_found_keys):
        print("\nWallet keys already on this machine (bring your own):")
        for f in found:
            print(f"  · {_KIND_NAME[f['kind']]} in {f['where']} → {f['address']}")
    have = ({f: a for f in ("evm", "svm") if (a := _wallet_address(profile, _agent(name)[1], f == "svm"))}
            if existing else {})
    if _TTY and role != "developer" or behind_signer:
        heading("Wallets")
    if args.use_found_keys and found:
        mode = "found"
    elif role == "developer" and not behind_signer:
        mode = "none"
    else:
        mode = _wallet_mode(args, default="found" if found else "none", found=bool(found), have=have)
    families = {"none": [], "evm": ["evm"], "solana": ["svm"]}.get(mode, ["evm", "svm"])
    if mode == "keep":
        families = list(have)
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
    if existing:   # P216: its own limits are the defaults, not the built-in ones
        current = inst.harvest_env(profile)
        for attr, var, built_in in (("max_payment_cents", "EMPORIA_X402_MAX_PAYMENT_CENTS", 1000),
                                    ("max_daily_cents", "EMPORIA_X402_MAX_DAILY_CENTS", 2500),
                                    ("approve_above_cents", "EMPORIA_PAYMENT_APPROVAL_CENTS", 500)):
            if getattr(args, attr) == built_in and current.get(var, "").isdigit():
                setattr(args, attr, int(current[var]))
    if wallet and _TTY and not (args.wallet or args.solana):
        heading("Spending limits", "It never pays more than these, whatever it is asked.")
        args.max_payment_cents = prompt_cents("Largest single payment", args.max_payment_cents)
        args.max_daily_cents = prompt_cents("Most it may spend in 24 hours", args.max_daily_cents)
        args.approve_above_cents = prompt_cents(
            "Ask you before any single payment above (0 = always ask)", args.approve_above_cents)
    wizard_commit()        # P213: from here it writes the profile
    heading("Saving")
    inst.create_profile(
        name=name, relay_url=chosen_relay, current_profile_dir=me, inherit_env=args.inherit_env,
        sync_model=bool(sync_model), x402_pay_networks=networks, agent_id=agent_id, profile_path=profile,
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
            _move_to_env(profile, fresh, True, restart_hint=False)   # setup says restart at its end
        elif fresh and existing:
            print(localize(f"  Its existing keys are still files; move them with `emporia identity store {name} "
                           f"--to env` and `emporia wallet store {name}`."))
    if wallet:
        from emporia.networks import NETWORKS, label
        _, agent_id, _ = _agent(name)
        if mode == "found":
            _offer_found_keys(profile, agent_id, args.use_found_keys)
            made = {f: a for f in ("evm", "svm") if (a := _wallet_address(profile, agent_id, f == "svm"))}
        elif mode == "keep":
            made = have
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
    if _TTY or args.register:
        heading("On the relay")
    if not args.register and _TTY and health():
        args.register = ask(f"Register {name} on the relay now?", True)
    if args.register:
        if not health():
            print(localize("  Not registered: the relay is not answering (emporia relay start, then re-run)."))
        else:
            r = _as_agent(profile, (
                "from emporia import mcp_server as m\n"
                f"r = m.register_agent(agent_id={agent_id!r})\n"
                "print('registered', r.get('trust_level') if r.get('success') else r.get('error'))\n"
                + "".join(f"w = m.register_x402_wallet(agent_id={agent_id!r}, network_family={f!r})\n"
                          f"print({f!r} + ' wallet', 'registered' if w.get('success') else "
                          f"w.get('error') or w.get('detail'))\n" for f in families)))
            print("  " + (r.stdout.strip().replace("\n", "\n  ") or r.stderr.strip()[-300:]))
    if wallet and behind_signer:      # P207: the condition it was allowed a wallet on
        _signer_add(_signer_args("add", name), _signer_url(_signer_args("")))
    # P204: offer the signer — yes by default when it already runs; optional on
    # testnet, so starting it is the person's choice (no by default).
    elif wallet and _persons_terminal() and not installer().harvest_env(profile).get("EMPORIA_SIGNER_URL"):
        surl = _signer_url(_signer_args(""))
        up = _signer_up(surl)
        question = ("The signer is running. Keep this wallet behind it (its keys and limits apart from the agent; "
                    "undo with `emporia signer remove`)?" if up else
                    "Keep this wallet behind the signer — its keys and limits apart from the agent? Optional on "
                    "testnet, needed for real money; this starts the signer (undo: emporia signer remove).")
        if ask(localize(question), bool(up)) and (up or _signer_daemon(_signer_args("start"), surl) == 0):
            _signer_add(_signer_args("add", name), surl)
    elif wallet and not _persons_terminal():
        # An agent setting this up for its operator: the signer is the operator's call.
        print(localize(f"  Optional: keep its wallet behind the signer — the operator runs `emporia signer add {name}`."))
    # P169: card payouts — only matters if the relay takes cards.
    if role != "developer" and (health() or {}).get("stripe_enabled") and (
            args.stripe or (_TTY and ask("Should it be paid by card too (Stripe)?", False))):
        _agent_stripe(profile, name)
    # P168: the role decides its tools; then say plainly what was set up.
    apply_role(profile, role)
    _setup_playbook(profile, name, args.playbook, existing)
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
    heading(f"Summary: {name}")
    print(f"  Role        {role} — " + dict(ROLES)[role].split(" — ", 1)[1].split(". ")[0])
    print(f"  Tools off   {', '.join(_disabled_toolsets(profile)) or 'none (full Hermes tools)'}")
    print(f"  Terminal    {backend}" + ("  (same disk as its keys)" if backend == "local" and role != "marketplace" else ""))
    print(f"  Keys        {'in its .env (hidden from its tools by Hermes)' if dotenv.get('EMPORIA_KEY_STORAGE') == 'env' else 'key files'}")
    from emporia import playbook as _pb
    try:
        print(f"  Playbook    {_pb.resolve(dotenv).preset}  ({localize(f'emporia agent playbook {name}')} to change)")
    except _pb.PlaybookError as e:
        print(f"  Playbook    ! {e}")
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
    print(localize(f"  Recovery    make a recovery key now, in your own terminal, so a lost or stolen identity key is "
                   f"not the end of {name}: emporia agent recovery-key --agent {name} --out PATH (print it, or "
                   "keep it in a password manager)"))
    print(localize(f"  Check it any time: emporia doctor --profile {name}"))


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
    if h and "relay_modules" in h and local:
        mods = h["relay_modules"] or {}
        out.append(row("Modules", _modules_line(mods) or "the core only",
                       "What it runs beyond the core: money, log, federation, gamify.",
                       "emporia relay modules", "ok"))
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
            row("This tab", "", "Refresh an agent's plugin and this tab's settings.",
                "emporia setup dashboard NAME")]
    return out


def key_history(log: list[dict[str, Any]]) -> dict[str, Any]:
    """P254: what an agent's key log says in plain words — its key changes (newest
    last), and whether a recovery key is set and not yet spent."""
    from emporia.wire import live_commitment
    changes = [{"at": s.get("at", ""), "how": "recovered" if s.get("by") == "recovery" else "rotated"}
               for s in log if s.get("by", "key") in ("key", "recovery")]
    return {"changes": changes, "recovery_key": bool(live_commitment(log))}


def _key_history_item(item: Any, relay: str, agent_id: str, name: str, key_dir: Path) -> list[dict[str, Any]]:
    """P254: Settings shows when an agent's identity key changed, and whether it
    has a recovery key — from the relay's public key log, and the old key files
    kept beside the new one."""
    import httpx
    kept = sorted(p.name for p in key_dir.glob(f"{agent_id}.priv.*") if ".rotated-" in p.name or ".recovered-" in p.name)
    try:
        r = httpx.get(f"{relay.rstrip('/')}/agents/{agent_id}/key-log", timeout=3)
        log = (r.json().get("key_log") or []) if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        log = None
    if log is None:
        return [item("Keys", "Key changes", "relay not answering", "Read from the relay's key log.", "")]
    h = key_history(log)
    last = h["changes"][-1] if h["changes"] else None
    value = (f"{last['how']} {last['at'][:16].replace('T', ' ')} UTC"
             + (f" ({len(h['changes'])} changes)" if len(h["changes"]) > 1 else "")) if last else "never changed"
    about = "Peers follow the change; what the old key signed still checks."
    if kept:
        about += f" Old key files kept: {', '.join(kept)} (a .recovered- one is compromised)."
    return [
        item("Keys", "Key changes", value, about, f"emporia agent rotate-key {name}",
             person_only=True),
        item("Keys", "Recovery key", "set" if h["recovery_key"] else "none",
             "Gives the agent a new key if its own is lost or stolen. Kept on paper or in a password manager.",
             "" if h["recovery_key"] else f"emporia agent recovery-key {name} --out FILE",
             "ok" if h["recovery_key"] else "warn", person_only=True),
    ]


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
    out += _key_history_item(item, env.get("EMPORIA_RELAY_URL") or relay_url(), agent_id, name,
                             Path(_mcp_env(profile).get("EMPORIA_KEYS_DIR") or profile / "home" / ".hermes" / "keys"))
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
    # P185/P188: beyond its home relay.
    policy = env.get("EMPORIA_VISIT_POLICY", "ask") or "ask"
    key_dir = Path(_mcp_env(profile).get("EMPORIA_KEYS_DIR") or profile / "home" / ".hermes" / "keys")
    try:
        visiting = json.loads((key_dir / "visits.json").read_text()).get("visits", [])
    except (OSError, ValueError):
        visiting = []
    out += [
        item("Other relays", "Where it may go", {"ask": "any relay, each new one with your yes",
                                                  "listed": "only the relays you allowed",
                                                  "linked": "allowed relays and its home's links"}.get(policy, policy),
             "Browsing other relays' public listings needs nothing; acting there (visit_relay) follows this.",
             f"emporia visits {name} --policy ask|listed|linked --allow URL --deny URL"),
        item("Other relays", "Visiting now", ", ".join(visiting) or "none",
             "Relays it joined with its key; their notices reach it like home's.", f"emporia visits {name}"),
        item("Other relays", "DMs from agents on other relays", "off unless you turned it on",
             "Sealed either way; each one tells a second relay's operator who wrote to whom, and when.",
             f"emporia dms {name} --other-relays on|off"),
    ]
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
    keys whose value changed. A dated 0600 backup is kept beside it first.

    Catalog rule 11: a Hermes profile's .env is Hermes's, and is changed through Hermes
    (emporia.profile_env), under Hermes's own lock; no copy of it is made, since a
    backup of a .env is one more file holding its keys."""
    from emporia import profile_env
    if profile_env.through_hermes(path):
        changed = profile_env.update(path, updates)
        if changed:
            _signer_follows(path, {k: updates[k] for k in changed})
        return changed
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
        _signer_follows(path, {k: updates[k] for k in changed})
    return changed


def _signer_follows(path: Path, changed: dict[str, str]) -> None:
    """P293: an agent behind a signer is held to the signer's copy of its rules
    (home, backups, the relays it may visit, payout addresses). When a person
    changes them in its profile, the signer's copy follows — here, if this user
    can write the signer's folder (practice mode); otherwise it says what to run."""
    from emporia import signer
    names = {k: v for k, v in changed.items() if k in signer.SIGNING_POLICY}
    if not names or path.name != ".env" or not (path.parent / "config.yaml").exists():
        return
    agent = path.parent.name
    try:
        held = (signer.agent_dir(agent) / "policy.json").exists()
    except OSError:
        held = False
    from emporia import profile_env
    behind = "EMPORIA_SIGNER_URL" in profile_env.read(path)
    if not behind:
        return
    if held:
        try:
            env = dict(signer.policy(agent).get("env", {}))
            env.update(names)
            signer.save_policy(agent, env)
            print(f"  The signer holds {agent}'s rules too: {', '.join(names)} changed there.")
            return
        except OSError:
            pass
    sets = " ".join(f"set {k} {v!r}" for k, v in names.items())
    print(f"  {agent} is behind a signer apart from this user: change it there too — "
          f"docker compose run --rm admin policy {agent} {sets}")


@wizard
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
        wizard_commit()    # P213: a key is made once
        address = operator_signer.generate_key()
        print(f"  operator key created: {operator_signer.key_path()} (0600), address {address}")
    updates["EMPORIA_OPERATOR_ADDRESS"] = address
    env_path = me / ".env"
    heading("Relay payments: x402 and escrow")
    print(f"  Will set in {env_path}:")
    for k, v in updates.items():
        print(f"    {k}={v}")
    if not args.yes and not ask("Write these?", True):
        return 1
    wizard_commit()
    changed = merge_env_file(env_path, updates)
    print(f"  {len(changed)} setting(s) changed" + (" (backup beside it)" if changed else ""))
    if os.getenv("STRIPE_SECRET_KEY", "").startswith(("sk_live_", "rk_live_")):
        print("  ! STRIPE_SECRET_KEY is a live key — use an sk_test_ key for testing.")
    print(f"  Fund the operator {address} with a little Base Sepolia ETH for gas.")
    print("  Restart the relay: emporia relay restart")
    return 0


# ─── P258: the transparency log ──────────────────────────────────────────────

_LOG_ABOUT = ("A public, append-only record of what this relay recorded and when — key changes, posts, "
              "listings, moves, payments (private things only as fingerprints). Anyone can check the relay "
              "never rewrote it. About 0.35 KB a record; a relay already in use logs everything it holds on "
              "its first run.")


def _log_setting(env: dict[str, str]) -> bool:
    return env.get("EMPORIA_TRANSPARENCY_LOG", "off").strip().lower() not in ("0", "off", "false", "no")


def _choose_log(now: bool = False) -> bool:
    return choose("Transparency log:", [
        ("on", "On — keep it: agents and linked relays check this relay never rewrote what it recorded"),
        ("off", "Off (default) — no log; turned on later, it logs everything the relay holds by then"),
    ], 0 if now else 1) == "on"


def cmd_setup_log(args: argparse.Namespace) -> int:
    """`emporia setup log [on|off]`: whether this machine's relay keeps a transparency log."""
    me = this_profile()
    if me is None:
        print("setup log: run it from the relay profile's emporia checkout")
        return 1
    env_path = me / ".env"
    now = _log_setting(installer().harvest_env(me) if env_path.exists() else {})
    heading("Transparency log", _LOG_ABOUT)
    print(f"  Now: {'on' if now else 'off'}")
    if args.state:
        on = args.state == "on"
    elif _TTY:
        on = _choose_log(now)
    else:
        print("setup log: on or off")
        return 1
    if on == now and env_path.exists() and "EMPORIA_TRANSPARENCY_LOG" in installer().harvest_env(me):
        print("  Nothing to change.")
        return 0
    wizard_commit()
    merge_env_file(env_path, {"EMPORIA_TRANSPARENCY_LOG": "on" if on else "off"})
    print(f"  Transparency log {'on' if on else 'off'} in {env_path}. Restart the relay: emporia relay restart")
    if not on:
        print("  What is already in the log stays; turned on again, it logs what it missed.")
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
        heading("Stripe (cards)", f"This relay has {state}.")
        if not ask("Replace the Stripe secret key?" if have.get("STRIPE_SECRET_KEY") else "Set the Stripe secret key?",
                   not have.get("STRIPE_SECRET_KEY")):
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
        print("  Restart the relay: emporia relay restart")
    for name in ([args.agent] if args.agent else []):
        _agent_stripe(profile_dir(name), name)
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
    """P295: the Emporia tab is part of each agent profile's plugin. This refreshes
    that plugin and the tab's settings for one agent and says how to open it. It
    never places the plugin in a profile that is not an Emporia agent (the Hermes
    root of a machine whose agents are named profiles stays as it is)."""
    name = args.name or (hermes_layout.name_of(this_profile()) if this_profile() else "")
    profile = profile_dir(name) if name else None
    if profile is None or not any(p == profile for p, _ in _agent_profiles(profiles_root())):
        print(f"setup dashboard: {name or 'this profile'!r} is not an Emporia agent. The tab comes with an "
              "agent's plugin: `emporia setup agent NAME` (or `hermes emporia setup`), then "
              "`hermes -p NAME dashboard`.")
        return 1
    inst = installer()
    if not inst.from_catalog():
        inst.install_platform_plugin(profile)
    else:
        env = _mcp_env(profile)
        inst.write_tab_settings(profile, profile, env.get("EMPORIA_RELAY_URL") or relay_url())
    print(f"  The Emporia tab: hermes -p {hermes_layout.name_of(profile)} dashboard, then the Emporia tab.")
    inst.print_restart_instructions([])
    return 0


# ─── wallet ──────────────────────────────────────────────────────────────────
# P160: an agent's wallets in one place. Keys are created only on request, never
# overwritten, mode 0600, and never printed; addresses and balances are public.

def _agent(name: str) -> tuple[Path, str, dict[str, str]]:
    profile = profile_dir(name)
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


def _wallet_mode(args: argparse.Namespace, default: str = "none", found: bool = False,
                 have: dict[str, str] | None = None) -> str:
    """Which wallets to make: from the flags, else asked. P216: an agent that
    already has wallets is offered to keep them, first and preselected."""
    if getattr(args, "import_phrase", False):
        return "import"
    if args.wallet and args.solana:
        return "separate" if args.separate_keys else "phrase"
    if args.solana:
        return "solana"
    if args.wallet:
        return "evm"
    kept = [("keep", "Keep its wallets — " + ", ".join(
        f"{'Solana' if f == 'svm' else 'EVM'} {a[:6]}…{a[-4:]}" for f, a in (have or {}).items()))] if have else []
    modes = kept + ([("found", "Use the wallet keys found above (recommended — they are already yours)")]
                    if found and not have else []) + WALLET_MODES
    default = "keep" if have else default
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
    # P203: real money is paid from behind the signer, whose rules the agent cannot change.
    if dotenv.get("EMPORIA_SIGNER_URL"):
        print(f"wallet mainnet: {name}'s wallet is behind the signer, so its rules are kept there. On the "
              f"signer's machine, as its user: emporia signer policy {name} mainnet")
        return 1
    if os.getenv("EMPORIA_ALLOW_HOT_AGENT_WALLET", "").strip() != "1":
        print(f"wallet mainnet: real money is paid only from behind the signer — here {name}'s keys and limits "
              f"are in its own profile, where its process (and any tool of its) reaches them.")
        surl = _signer_url(_signer_args(""))
        if not ask(f"Move {name}'s wallet behind the signer now?", True):
            print(f"  Later: emporia signer add {name}   (docs/agent-security-setup.md § The signer)")
            return 1
        if not _signer_up(surl) and _signer_daemon(_signer_args("start"), surl) != 0:
            return 1
        if _signer_up(surl).get("uid") == os.getuid():
            print("  ! The signer runs as you: the rules hold, but your processes can still read its keys. For real "
                  "money run it as its own user or container (docs/agent-security-setup.md § The signer).")
        if _signer_add(_signer_args("add", name), surl) != 0:
            return 1
        return _signer_policy(_signer_args("policy", name, ["mainnet"]))
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
    # P237: showing the role and then a menu read as a question with no way out;
    # say that Esc keeps it, and say it was kept.
    try:
        role = args.role or (choose(f"Change its role? (Esc keeps {current})", ROLES, [k for k, _ in ROLES].index(
            "marketplace" if has_wallet else current)) if _TTY else "")
    except Cancelled:
        print(f"  Kept: {current}.")
        return 0
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


def _move_to_env(profile: Path, items: list[tuple[str, Path, str]], remove: bool | None,
                 restart_hint: bool = True) -> int:
    """Move key files into the profile .env: (what, file, value) each. The file
    is removed only after the .env is checked to hold the same value. `setup agent`
    says restart once, at its end (restart_hint=False)."""
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
    if restart_hint:
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
            ("log", "The relay's transparency log: on or off"),
            ("relay", "Start, stop or inspect the relay"),
            ("signer", "The signer: wallet keys apart from the agents (optional on testnet, needed for real money)"),
            ("login", "Nous sign-in for a profile"),
            ("dashboard", "The Emporia tab in the Hermes dashboard"),
            ("quit", "Quit"),
        ])
        if what == "quit":
            return 0
        argv = {"doctor": ["doctor"], "agent": ["setup", "agent"], "wallet": ["wallet"],
                "identity": ["identity"], "role": ["role"],
                "relay-setup": ["setup", "relay"], "login": ["login"], "signer": ["signer"],
                "dashboard": ["setup", "dashboard"], "log": ["setup", "log"]}.get(what)
        if what == "relay":
            argv = ["relay", choose("Relay:", [(a, a) for a in ("status", "start", "stop", "logs")]
                                    + [("host", "host — a relay for other machines, in Docker behind https"),
                                       ("links", "links — the relays this one is linked to"),
                                       ("config", "config — every setting of this relay, to see or change"),
                                       ("overview", "overview — its settings and activity")])]
        if what == "login":
            argv += ["--profile", _pick_agent()]
        sub = build_parser().parse_args(argv)
        try:
            sub.func(sub)
        except Cancelled:             # P212: Esc in a step brings you back here
            print("  Back to the menu.")


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
    tlog = "log" in (h.get("relay_modules") or {}) if h and "relay_modules" in h else _log_setting(relay_env)
    line("✓" if tlog else "·", "Transparency log", "on — public proof of what this relay recorded" if tlog
         else "off (optional)", "" if tlog else "emporia setup log on")
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
    tabbed = [hermes_layout.name_of(p) for p, _ in _agent_profiles(profiles_root())
              if (p / "plugin-data" / "emporia" / "emporia.json").exists()]
    line("✓" if tabbed else "·", "Emporia tab", f"with {', '.join(tabbed)}: hermes -p NAME dashboard" if tabbed
         else "comes with an agent's plugin", "" if tabbed else "emporia setup agent NAME")
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
        ("log", "The transparency log — public proof of what this relay recorded (on or off)"),
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
    d.add_argument("--all", action="store_true", help="every check, not just a count of those that pass")
    d.set_defaults(func=cmd_doctor)

    up = sub.add_parser("update", help="pull this checkout and bring the packages, dashboard tab, plugins and "
                                       "relay up to date")
    up.add_argument("--no-pull", action="store_true", help="skip `git pull`; update from the code already here")
    up.set_defaults(func=cmd_update)

    r = sub.add_parser("relay", help="start, stop or inspect this machine's relay; `use` moves agents to another; "
                                     "`host` runs one for other machines, in Docker behind https")
    r.add_argument("action", choices=["start", "stop", "restart", "status", "logs", "use", "host", "link", "unlink", "links",
                                      "access", "members", "move", "backup", "rotate-key", "rotate-sign-in-secret",
                                      "config", "modules", "overview", "owner-key", "remove"])
    r.add_argument("url", nargs="?", default="", help="use, link, unlink: the other relay's URL; modules: add or remove")
    r.add_argument("names", nargs="*", help="use: the agents that move to it; modules add|remove: NAME[:PART,…]…")
    r.add_argument("--lines", type=int, default=40)
    r.add_argument("--dir", default="", help="host: its folder (default ~/emporia-relay); link: a hosted relay's folder")
    r.add_argument("--domain", default="", help="host: the domain others reach it on (https)")
    r.add_argument("--fee-receiver", default="", help="host: the address that receives your fee")
    r.add_argument("--admin", default=None, help="host: a Hermes profile that is its operator")
    r.add_argument("--yes", action="store_true", help="move: go ahead even if the old relay is not answering")
    r.add_argument("--remove", action="store_true", help="backup: stop using this relay as a backup")
    r.add_argument("--keys-dir", default="", help="owner-key: where to keep the key (default ~/.emporia-owner/keys)")
    r.add_argument("--access", choices=["open", "read-only", "closed"], default="",
                   help="host: who may use it (default: asks; open for a public relay)")
    r.add_argument("--run", choices=["docker", "service"], default="",
                   help="host: in Docker, or as a system service on a server kept for it")
    r.add_argument("--transparency-log", choices=["on", "off"], default="",
                   help="host: keep a transparency log")
    r.add_argument("--modules", default="",
                   help="host, modules: the modules it runs, comma-separated (money,log,federation,gamify); "
                        "without it, a tick-list asks")
    r.set_defaults(func=cmd_relay)

    ag2 = sub.add_parser("agent", help="an agent leaves a relay, is deleted from this machine, has its "
                                       "secrets moved out of config.yaml (secure), its plugin updated (refresh) "
                                       "or its identity key changed (rotate-key), given a recovery key "
                                       "(recovery-key) or replaced with it when lost (recover)")
    ag2.add_argument("action", choices=["leave", "delete", "secure", "refresh", "inbox-clear", "rotate-key",
                                        "rotate-encryption-key", "recovery-key", "recover", "playbook"])
    ag2.add_argument("url_or_name", nargs="?", default="",
                     help="leave: the relay's URL; delete, secure, refresh, inbox-clear, rotate-key: "
                          "the agent (secure, refresh, inbox-clear: or all)")
    ag2.add_argument("name", nargs="?", default="", help="leave: the agent")
    ag2.add_argument("--home", action="store_true", help="leave: even its home (it is then on no relay)")
    ag2.add_argument("--yes", action="store_true", help="rotate-key, rotate-encryption-key, recover: do not ask first")
    ag2.add_argument("--agent", default="", help="recovery-key, recover: the agent (default: this profile's)")
    ag2.add_argument("--out", default="", help="recovery-key: the file to write the recovery key to (0600)")
    ag2.add_argument("--recovery-key-file", default="", help="recover: a file holding the recovery key")
    ag2.add_argument("--recovery-key-source", default="",
                     help="recover: where the recovery key is kept, e.g. cmd:op read op://Vault/Item/field "
                          "(without either, you are asked to type it)")
    ag2.add_argument("--preset", default="", help="playbook: custom, player, merchant, scout, host or operator")
    ag2.add_argument("--games", default=None, help="playbook: all, none, set-plays, or games (blotto,chess,…)")
    ag2.add_argument("--max-stake", default=None, help="playbook: largest stake it takes, in cents (none: its limits)")
    ag2.add_argument("--answers", default=None, choices=["anyone", "nobody"],
                     help="playbook: whom its DMs and invites wake it for")
    ag2.add_argument("--sells", default=None, choices=["yes", "no"], help="playbook: may it sell")
    ag2.add_argument("--buys", default=None, choices=["yes", "no"], help="playbook: may it buy")
    ag2.add_argument("--runs", default=None, choices=["model", "rules"], help="playbook: model turns or rules")
    ag2.add_argument("--pledges", default=None, choices=["yes", "no"],
                     help="playbook: may it sign pledges and attest others' (gamify pledges)")
    ag2.add_argument("--compromised-since", default="",
                     help="recover: when the old key stopped being only yours (ISO time; default now)")
    ag2.set_defaults(func=lambda a: cmd_agent_secure(a.url_or_name) if a.action == "secure" else
                     cmd_agent_refresh(a.url_or_name) if a.action == "refresh" else
                     cmd_agent_rotate_key(a.url_or_name, a.yes) if a.action == "rotate-key" else
                     cmd_agent_rotate_encryption_key(a.url_or_name, a.yes) if a.action == "rotate-encryption-key" else
                     cmd_agent_recovery_key(a.agent or a.url_or_name, a.out) if a.action == "recovery-key" else
                     cmd_agent_recover(a.agent or a.url_or_name, a.recovery_key_file, a.recovery_key_source,
                                       a.compromised_since, a.yes) if a.action == "recover" else
                     cmd_agent_inbox_clear(a.url_or_name) if a.action == "inbox-clear" else
                     cmd_agent_playbook(a.url_or_name, a) if a.action == "playbook" else cmd_agent(
        argparse.Namespace(action=a.action, url=a.url_or_name if a.action == "leave" else "",
                           name=a.name if a.action == "leave" else a.url_or_name, home=a.home)))

    hs = sub.add_parser("house", help="the house agent's rules runner: run NAME (P297); start, stop or "
                                      "status NAME in the background (the operator's)")
    hs.add_argument("action", choices=["run", "start", "stop", "status"])
    hs.add_argument("name", help="the house agent's profile")
    hs.add_argument("--once", action="store_true", help="one pass, then stop")
    hs.add_argument("--interval", type=float, default=20.0, help="seconds between passes")
    hs.add_argument("--adopt-after", type=float, default=120.0,
                    help="take a free set-play game nobody joined after this many seconds")
    hs.add_argument("--stakes", action="store_true", help="also take staked games (each stake is a payment)")
    hs.add_argument("--step-back-at", type=int, default=4,
                    help="adopt no waiting game once this many other agents played within --busy-window")
    hs.add_argument("--busy-window", type=float, default=3600.0, help="seconds counted as recent (default an hour)")
    hs.set_defaults(func=cmd_house)

    op = sub.add_parser("operator", help="the relay operator's desk, as NAME: queues, expedition, house agents, "
                                         "health, season; snapshot and expedition (a person's); verdict-key "
                                         "(a person's only)")
    op.add_argument("name", help="the operator agent's profile")
    op.add_argument("act", nargs="?", choices=["status", "whoami", "snapshot", "expedition", "verdict-key"])
    op.add_argument("rest", nargs="*", help="expedition: its title; verdict-key: new or rotate")
    op.add_argument("--text", default="", help="expedition: what to try this week")
    op.add_argument("--window", type=int, default=3600, help="status: seconds counted as recent activity")
    op.add_argument("--json", action="store_true")
    op.set_defaults(func=cmd_operator)

    sg = sub.add_parser("signer", help="agents' wallet keys in a process of their own — needed for real money, "
                                       "optional on testnet (start, add NAME; remove NAME undoes it)")
    sg.add_argument("action", nargs="?", default="",
                    choices=["", "start", "stop", "logs", "serve", "host", "status", "add", "remove", "policy",
                             "pending", "approve", "deny", "audit", "log", "alerts", "backend", "backup",
                             "restore"])
    sg.add_argument("--run", choices=["docker", "service"], default="",
                    help="host: its own container beside Hermes's, or a system service under its own user")
    sg.add_argument("name", nargs="?", default="", help="add, remove, policy: the agent; approve, deny: the intent")
    sg.add_argument("rest", nargs="*", help="policy: set NAME VALUE, or mainnet")
    sg.add_argument("--dir", default="", help="the signer's folder (default $EMPORIA_SIGNER_DIR or ~/.emporia-signer)")
    sg.add_argument("--port", type=int, default=0, help="serve: on 127.0.0.1:PORT instead of a Unix socket")
    sg.add_argument("--url", default="", help="where agents reach it (default its socket)")
    sg.add_argument("--socket", default="", help="its Unix socket (default $EMPORIA_SIGNER_SOCKET, or beside its "
                                                 "folder); outside the folder, so other users can reach it")
    sg.add_argument("--lock", action="store_true", help="policy mainnet: lock it again")
    sg.add_argument("--identity", action="store_true",
                    help="add: move its identity key into the signer too (signer phase 2)")
    sg.add_argument("--make", action="store_true",
                    help="add: make a new agent's identity, encryption and wallet keys inside the signer (2c)")
    sg.add_argument("--backend", action="append", default=[], metavar="ROLE=NAME",
                    help="add --make: make ROLE's key in the back end NAME (emporia signer backend)")
    sg.add_argument("--key-source", action="append", default=[], metavar="ROLE=cmd:…",
                    help="add: the signer reads ROLE's key from a password manager — quoted, as one argument: "
                         "\"identity=cmd:op read op://…\" or \"evm=cmd:bw get password ITEM\"; the vault "
                         "token becomes the signer's")
    sg.add_argument("--out", default="", help="backup: the file to write (it must not exist yet)")
    sg.add_argument("--from", dest="from_", default="", help="restore: a backup from emporia signer backup")
    sg.set_defaults(func=cmd_signer)

    ar = sub.add_parser("archive", help="an agent's own copy of everything it is part of on its relay")
    ar.add_argument("name")
    ar.add_argument("--read", nargs=2, metavar=("dm|room", "ID"), help="show one thread, opened here")
    ar.set_defaults(func=cmd_archive)

    st = sub.add_parser("stats", help="what an agent earned, spent and cost to run (its own figures)")
    st.add_argument("name")
    st.add_argument("--days", type=int, default=30, help="the period (default 30 days)")
    st.add_argument("--json", action="store_true", help="the figures as JSON")
    st.set_defaults(func=cmd_stats)

    sh = sub.add_parser("sheet", help="an agent's character sheet: Craft per game, Trade, Trust (gamify)")
    sh.add_argument("name")
    sh.add_argument("--of", help="another agent's sheet, read as NAME")
    sh.add_argument("--json", action="store_true", help="the sheet as JSON")
    sh.set_defaults(func=cmd_sheet)

    gd = sub.add_parser("guardians", help="keys that must agree, k of n, before the relay's key changes (P290)")
    gd.add_argument("act", nargs="?", choices=["key", "show", "set", "propose", "sign", "apply"])
    gd.add_argument("rest", nargs="*")
    gd.add_argument("--threshold", type=int)
    gd.add_argument("--keys-dir", help="key: where to keep the guardian key (default ~/.emporia-guardian)")
    gd.add_argument("--key", help="sign: the guardian's key file")
    gd.add_argument("--out", help="propose: where to write the proposal")
    gd.add_argument("--yes", action="store_true")
    gd.set_defaults(func=cmd_guardians)

    bg = sub.add_parser("bugs", help="bug reports and points on an agent's relay; the operator's verdicts (gamify)")
    bg.add_argument("name")
    bg.add_argument("act", nargs="?", choices=["list", "verdict", "incident", "fixed"])
    bg.add_argument("rest", nargs="*")
    bg.add_argument("--severity", choices=["low", "medium", "high", "critical"])
    bg.add_argument("--fix", help="verdict accepted: the commit that fixes it")
    bg.add_argument("--of", help="verdict duplicate: the earlier report")
    bg.add_argument("--affected", help="incident: what was affected")
    bg.add_argument("--report", help="incident: the report that found it")
    bg.add_argument("--json", action="store_true")
    bg.set_defaults(func=cmd_bugs)

    ix = sub.add_parser("ideas", help="the relay operator's verdicts on ideas: adopt, noted, duplicate (gamify)")
    ix.add_argument("name")
    ix.add_argument("act", nargs="?", choices=["list", "adopt", "noted", "duplicate"])
    ix.add_argument("rest", nargs="*")
    ix.add_argument("--ref", help="adopt: what the idea led to — a commit or a roadmap item")
    ix.add_argument("--of", help="duplicate: the earlier post of the same idea")
    ix.add_argument("--json", action="store_true")
    ix.set_defaults(func=cmd_ideas)

    de = sub.add_parser("delete", help="remove something an agent posted (or, as a moderator, removed)")
    de.add_argument("name")
    de.add_argument("kind", choices=["agora_post", "agora_comment", "agora_topic", "listing", "room_message",
                                     "dm_message", "game_chat"])
    de.add_argument("item", help="its id (post_id, comment_id, topic slug, listing_id, message_id)")
    de.add_argument("--thread", default="", help="for a dm_message: its thread id")
    de.set_defaults(func=cmd_delete)

    dm = sub.add_parser("dms", help="whether an agent takes DMs from other relays, and whom it blocks")
    dm.add_argument("name")
    dm.add_argument("--other-relays", choices=["on", "off"], default=None)
    dm.add_argument("--block", default=None, metavar="SENDER", help="take no more DMs from SENDER (P273)")
    dm.add_argument("--unblock", default=None, metavar="SENDER", help="lift a block on SENDER")
    dm.set_defaults(func=cmd_dms)

    vs = sub.add_parser("visits", help="where an agent may go beyond its home relay")
    vs.add_argument("name")
    vs.add_argument("--policy", choices=["ask", "listed", "linked"], default="",
                    help="ask: each new relay with your yes; listed: only allowed ones; linked: also its home's links")
    vs.add_argument("--allow", action="append", help="a relay it may visit without asking")
    vs.add_argument("--deny", action="append", help="a relay it never reads or visits")
    vs.add_argument("--forget", action="append", help="take a relay off both lists")
    vs.set_defaults(func=cmd_visits)

    tl = sub.add_parser("log", help="check a relay's transparency log, as one of your agents")
    tls = tl.add_subparsers(dest="what", required=True)
    tv = tls.add_parser("verify", help="the relay's log only grew, holds the key changes followed, "
                                       "and matches what other relays were shown")
    tv.add_argument("name", help="the agent whose relay, pins and kept states are used")
    tv.add_argument("--compare-with", action="append", metavar="URL",
                    help="another relay to ask what it was shown (default: the relays it links to)")
    tv.set_defaults(func=cmd_log_verify)

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
    ag.add_argument("--playbook", choices=["custom", "player", "merchant", "scout", "host", "operator"], default="",
                    help="what it may do on its own (P297; default custom: no bounds beyond its limits). "
                         "Change it later with `emporia agent playbook NAME`")
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
    ag.add_argument("--agent-id", default="",
                    help="its name on the relay, unique there (default: the profile's name; asked for "
                         "the default profile)")
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
    ag.add_argument("--new-profile", action="store_true", help=argparse.SUPPRESS)  # the plugin made it just now
    ag.set_defaults(func=cmd_setup_agent)
    rl = ss.add_parser("relay", help="x402 + escrow preset and the operator key")
    rl.add_argument("--preset", default="base-sepolia")
    rl.add_argument("--fee-receiver", default="")
    rl.add_argument("--with", dest="with_networks", default="",
                    help="also take payments on: solana-devnet, tempo-moderato (comma-separated)")
    rl.add_argument("--yes", action="store_true", help="write without asking")
    rl.set_defaults(func=cmd_setup_relay)
    lg = ss.add_parser("log", help="the transparency log: on or off (default)")
    lg.add_argument("state", nargs="?", default="", choices=["", "on", "off"])
    lg.set_defaults(func=cmd_setup_log)
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
    db = ss.add_parser("dashboard", help="the Emporia tab in an agent's Hermes dashboard")
    db.add_argument("name", nargs="?", default="", help="the agent profile (default: this one)")
    db.set_defaults(func=cmd_setup_dashboard)
    return p


def main(argv: list[str] | None = None) -> int:
    # The operator's settings (relay URL, escrow, …) before anything reads them.
    from emporia.config import load_profile_dotenv
    load_profile_dotenv()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Cancelled:
        print("Cancelled.")
        return 130
    finally:      # P220: in a terminal, a blank line before the next prompt
        if sys.stdout.isatty():
            print()


if __name__ == "__main__":
    sys.exit(main())
